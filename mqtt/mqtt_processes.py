"""Contain Windows descendants even when the runner is forcibly terminated."""
import os

_JOB = None


def contain_children():
    global _JOB
    if os.name != 'nt' or _JOB is not None:
        return
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [('ProcessTime', c.c_int64), ('JobTime', c.c_int64), ('Flags', w.DWORD),
                    ('MinWS', c.c_size_t), ('MaxWS', c.c_size_t), ('Active', w.DWORD),
                    ('Affinity', c.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]
    class Counters(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]
    class Extended(c.Structure):
        _fields_ = [('Basic', Basic), ('IO', Counters), ('ProcessMemory', c.c_size_t),
                    ('JobMemory', c.c_size_t), ('PeakProcessMemory', c.c_size_t), ('PeakJobMemory', c.c_size_t)]
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise c.WinError(c.get_last_error())
    limits = Extended()
    limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)):
        error = c.get_last_error()
        kernel.CloseHandle(handle)
        raise c.WinError(error)
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        error = c.get_last_error()
        kernel.CloseHandle(handle)
        raise c.WinError(error)
    # Non-inheritable handle stays alive until process exit. Closing it earlier would
    # kill the runner too. Windows closes it on graceful exit AND TerminateProcess.
    _JOB = handle
