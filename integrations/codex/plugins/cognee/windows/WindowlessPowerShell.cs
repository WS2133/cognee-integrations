using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// Codex currently launches every command hook through its detected shell. On
// native Windows that parent console process can flash even when the command it
// runs is windowless. Codex probes `pwsh` before Windows PowerShell, so this
// Codex-only GUI-subsystem launcher gives it the same PowerShell 5.1 behavior
// without allocating a console window.
internal static class WindowlessPowerShell
{
    private const uint Th32csSnapProcess = 0x00000002;
    private static readonly IntPtr InvalidHandleValue = new IntPtr(-1);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
    private struct ProcessEntry32
    {
        public uint Size;
        public uint Usage;
        public uint ProcessId;
        public IntPtr DefaultHeapId;
        public uint ModuleId;
        public uint Threads;
        public uint ParentProcessId;
        public int BasePriority;
        public uint Flags;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string ExecutableFile;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);

    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern bool Process32First(IntPtr snapshot, ref ProcessEntry32 entry);

    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern bool Process32Next(IntPtr snapshot, ref ProcessEntry32 entry);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    private static int Main(string[] args)
    {
        if (!IsCodexInvocation())
        {
            Console.Error.WriteLine(
                "This windowless PowerShell launcher is reserved for Codex hooks."
            );
            return 127;
        }

        string systemRoot = Environment.GetEnvironmentVariable("SystemRoot");
        if (String.IsNullOrEmpty(systemRoot))
        {
            systemRoot = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        }

        string powershell = Path.Combine(
            systemRoot,
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe"
        );
        if (!File.Exists(powershell))
        {
            Console.Error.WriteLine("Windows PowerShell was not found at " + powershell);
            return 127;
        }

        try
        {
            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = powershell;
            startInfo.Arguments = JoinArguments(args);
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.RedirectStandardInput = true;
            startInfo.RedirectStandardOutput = true;
            startInfo.RedirectStandardError = true;

            using (Process process = Process.Start(startInfo))
            {
                if (process == null)
                {
                    Console.Error.WriteLine("Windows PowerShell did not start.");
                    return 1;
                }

                Thread outputPump = StartPump(
                    process.StandardOutput.BaseStream,
                    Console.OpenStandardOutput(),
                    false
                );
                Thread errorPump = StartPump(
                    process.StandardError.BaseStream,
                    Console.OpenStandardError(),
                    false
                );
                StartPump(
                    Console.OpenStandardInput(),
                    process.StandardInput.BaseStream,
                    true
                );

                process.WaitForExit();
                outputPump.Join();
                errorPump.Join();
                return process.ExitCode;
            }
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("Could not start Windows PowerShell: " + exception.Message);
            return 1;
        }
    }

    private static Thread StartPump(Stream source, Stream destination, bool closeDestination)
    {
        Thread thread = new Thread(delegate()
        {
            try
            {
                source.CopyTo(destination);
                destination.Flush();
            }
            catch (IOException)
            {
                // A peer can close a hook pipe as its process exits.
            }
            catch (ObjectDisposedException)
            {
                // The PowerShell child may exit before the stdin pump finishes.
            }
            finally
            {
                if (closeDestination)
                {
                    try
                    {
                        destination.Close();
                    }
                    catch (IOException)
                    {
                    }
                }
            }
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }

    private static bool IsCodexInvocation()
    {
        if (!String.IsNullOrEmpty(
                Environment.GetEnvironmentVariable("CODEX_INTERNAL_ORIGINATOR_OVERRIDE")
            ) ||
            !String.IsNullOrEmpty(Environment.GetEnvironmentVariable("CODEX_THREAD_ID")))
        {
            return true;
        }

        return HasCodexAncestor();
    }

    private static bool HasCodexAncestor()
    {
        IntPtr snapshot = CreateToolhelp32Snapshot(Th32csSnapProcess, 0);
        if (snapshot == InvalidHandleValue)
        {
            return false;
        }

        try
        {
            Dictionary<uint, ProcessEntry32> processes = new Dictionary<uint, ProcessEntry32>();
            ProcessEntry32 entry = new ProcessEntry32();
            entry.Size = (uint)Marshal.SizeOf(typeof(ProcessEntry32));
            if (Process32First(snapshot, ref entry))
            {
                do
                {
                    processes[entry.ProcessId] = entry;
                    entry.Size = (uint)Marshal.SizeOf(typeof(ProcessEntry32));
                }
                while (Process32Next(snapshot, ref entry));
            }

            uint processId = (uint)Process.GetCurrentProcess().Id;
            for (int depth = 0; depth < 16; depth++)
            {
                ProcessEntry32 current;
                if (!processes.TryGetValue(processId, out current) || current.ParentProcessId == 0)
                {
                    return false;
                }

                ProcessEntry32 parent;
                if (!processes.TryGetValue(current.ParentProcessId, out parent))
                {
                    return false;
                }

                string name = (parent.ExecutableFile ?? String.Empty).ToLowerInvariant();
                if (name == "codex.exe" ||
                    name == "codex-command-runner.exe" ||
                    name == "codex-code-mode-host.exe" ||
                    name == "chatgpt.exe")
                {
                    return true;
                }

                processId = current.ParentProcessId;
            }

            return false;
        }
        finally
        {
            CloseHandle(snapshot);
        }
    }

    private static string JoinArguments(string[] args)
    {
        StringBuilder commandLine = new StringBuilder();
        for (int index = 0; index < args.Length; index++)
        {
            if (index > 0)
            {
                commandLine.Append(' ');
            }

            commandLine.Append(QuoteArgument(args[index]));
        }

        return commandLine.ToString();
    }

    // Windows command-line quoting compatible with CommandLineToArgvW.
    private static string QuoteArgument(string argument)
    {
        if (argument.Length == 0)
        {
            return "\"\"";
        }

        if (argument.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
        {
            return argument;
        }

        StringBuilder quoted = new StringBuilder();
        quoted.Append('"');
        int backslashes = 0;
        foreach (char character in argument)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }

            if (character == '"')
            {
                quoted.Append('\\', backslashes * 2 + 1);
                quoted.Append('"');
                backslashes = 0;
                continue;
            }

            quoted.Append('\\', backslashes);
            backslashes = 0;
            quoted.Append(character);
        }

        quoted.Append('\\', backslashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }
}
