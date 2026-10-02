using System.Diagnostics;
using System.Runtime.InteropServices;

namespace OmniSight.Core.Hosting;

/// <summary>
/// A Windows Job Object that kills everything in it when its handle closes, that is, when this app ends however it ends.
/// Only the Python client (and what it has started) goes in: the app itself stays out, so a browser opened from a link or
/// Explorer opened from "Open logs" is never killed with it.
/// </summary>
public static class ProcessJob
{
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint KillOnJobClose = 0x2000;
    private const uint ProcessSetQuotaAndTerminate = 0x0100 | 0x0001;
    private const uint SnapProcess = 0x2;
    private static readonly object Gate = new();
    private static IntPtr _job;

    /// <summary>
    /// Put <paramref name="process"/> and every descendant it has already started into the job. The venv's python.exe is only a
    /// launcher that starts the real interpreter at once, so the children are looked up and assigned too; anything they start
    /// afterwards inherits the job.
    /// </summary>
    public static bool Assign(Process process)
    {
        if (!OperatingSystem.IsWindows())
        {
            return false;
        }

        var job = EnsureJob();
        if (job == IntPtr.Zero)
        {
            return false;
        }

        var ok = AssignProcessToJobObject(job, process.Handle);
        for (var pass = 0; pass < 3; pass++)
        {
            foreach (var pid in Descendants(process.Id))
            {
                var handle = OpenProcess(ProcessSetQuotaAndTerminate, false, (uint)pid);
                if (handle == IntPtr.Zero)
                {
                    continue;
                }

                try
                {
                    AssignProcessToJobObject(job, handle);  // already in the job (inherited) is fine
                }
                finally
                {
                    CloseHandle(handle);
                }
            }
        }

        return ok;
    }

    /// <summary>Whether <paramref name="process"/> is in OmniSight's own job (not merely in some job).</summary>
    public static bool Contains(Process process)
    {
        lock (Gate)
        {
            return _job != IntPtr.Zero && IsProcessInJob(process.Handle, _job, out var inJob) && inJob;
        }
    }

    /// <summary>The pids of every process started (directly or not) by <paramref name="rootPid"/>.</summary>
    public static IReadOnlyList<int> Descendants(int rootPid)
    {
        var parents = new Dictionary<int, List<int>>();
        var snapshot = CreateToolhelp32Snapshot(SnapProcess, 0);
        if (snapshot == IntPtr.Zero || snapshot == new IntPtr(-1))
        {
            return [];
        }

        try
        {
            var entry = new ProcessEntry { Size = (uint)Marshal.SizeOf<ProcessEntry>() };
            for (var more = Process32First(snapshot, ref entry); more; more = Process32Next(snapshot, ref entry))
            {
                if (!parents.TryGetValue((int)entry.ParentProcessId, out var list))
                {
                    parents[(int)entry.ParentProcessId] = list = [];
                }

                list.Add((int)entry.ProcessId);
            }
        }
        finally
        {
            CloseHandle(snapshot);
        }

        var found = new List<int>();
        var queue = new Queue<int>([rootPid]);
        while (queue.Count > 0)
        {
            var current = queue.Dequeue();
            if (!parents.TryGetValue(current, out var children))
            {
                continue;
            }

            foreach (var child in children)
            {
                if (child != rootPid && !found.Contains(child))
                {
                    found.Add(child);
                    queue.Enqueue(child);
                }
            }
        }

        return found;
    }

    private static IntPtr EnsureJob()
    {
        lock (Gate)
        {
            if (_job != IntPtr.Zero)
            {
                return _job;
            }

            var job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
            {
                return IntPtr.Zero;
            }

            var info = new ExtendedLimits { Basic = { LimitFlags = KillOnJobClose } };
            var size = Marshal.SizeOf<ExtendedLimits>();
            var buffer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(info, buffer, false);
                if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, buffer, (uint)size))
                {
                    CloseHandle(job);
                    return IntPtr.Zero;
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }

            _job = job;  // never closed on purpose: it closes when this process ends, which kills the Python client
            return _job;
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimits
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimits
    {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ProcessEntry
    {
        public uint Size;
        public uint Usage;
        public uint ProcessId;
        public UIntPtr DefaultHeapId;
        public uint ModuleId;
        public uint Threads;
        public uint ParentProcessId;
        public int PriorityClassBase;
        public uint Flags;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string ExeFile;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsProcessInJob(IntPtr process, IntPtr job, [MarshalAs(UnmanagedType.Bool)] out bool result);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32First(IntPtr snapshot, ref ProcessEntry entry);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32Next(IntPtr snapshot, ref ProcessEntry entry);
}
