import sys, time
from rpython.rtyper.lltypesystem import rffi, lltype
from rpython.rtyper.tool import rffi_platform
from rpython.translator.tool.cbuild import ExternalCompilationInfo
from rpython.rtyper.extregistry import replacement_for

from rpython.rlib.rarithmetic import intmask

if sys.platform == 'win32':
    _WIN32 = True
    TIME_H = 'time.h'
    FTIME = '_ftime64'
    STRUCT_TIMEB = 'struct __timeb64'
    includes = ['winsock2.h', 'windows.h',
                TIME_H, 'sys/types.h', 'sys/timeb.h']
    need_rusage = False
else:
    _WIN32 = False
    TIME_H = 'sys/time.h'
    FTIME = 'ftime'
    STRUCT_TIMEB = 'struct timeb'
    includes = [TIME_H, 'time.h', 'errno.h', 'sys/select.h',
                'sys/types.h', 'unistd.h',
                'sys/time.h', 'sys/resource.h']

    if not sys.platform.startswith("openbsd"):
        includes.append('sys/timeb.h')

    need_rusage = True

eci = ExternalCompilationInfo(
    includes=includes
)

if sys.platform.startswith('freebsd') or sys.platform.startswith('netbsd'):
    libraries = ['compat']
elif sys.platform == 'linux2':
    libraries = ['rt']
else:
    libraries = []

class CConfig:
    _compilation_info_ = eci
    TIMEVAL = rffi_platform.Struct('struct timeval', [('tv_sec', rffi.INT),
                                                      ('tv_usec', rffi.INT)])
    HAVE_GETTIMEOFDAY = rffi_platform.Has('gettimeofday')
    HAVE_FTIME = rffi_platform.Has(FTIME)
    if need_rusage:
        RUSAGE = rffi_platform.Struct('struct rusage', [('ru_utime', TIMEVAL),
                                                        ('ru_stime', TIMEVAL)])

    TIMEB = rffi_platform.Struct(STRUCT_TIMEB, [('time', rffi.INT),
                                                ('millitm', rffi.INT)])

constant_names = ['RUSAGE_SELF', 'EINTR', 'CLOCK_PROCESS_CPUTIME_ID']
for const in constant_names:
    setattr(CConfig, const, rffi_platform.DefinedConstantInteger(const))
defs_names = ['GETTIMEOFDAY_NO_TZ']
for const in defs_names:
    setattr(CConfig, const, rffi_platform.Defined(const))

globals().update(rffi_platform.configure(CConfig))
TIMEVALP = lltype.Ptr(TIMEVAL)

def external(name, args, result, **kwargs):
    return rffi.llexternal(name, args, result, compilation_info=eci, **kwargs)

if HAVE_GETTIMEOFDAY:
    if GETTIMEOFDAY_NO_TZ:
        c_gettimeofday = external('gettimeofday',
                         [TIMEVALP], rffi.INT,
                          _nowrapper=True, releasegil=False)
    else:
        c_gettimeofday = external('gettimeofday',
                         [TIMEVALP, rffi.VOIDP], rffi.INT,
                          _nowrapper=True, releasegil=False)

# On some systems (e.g. SCO ODT 3.0) gettimeofday() may
# fail, so we fall back on ftime() or time().
if HAVE_FTIME:
    c_ftime = external(FTIME, [lltype.Ptr(TIMEB)],
                       lltype.Void,
                       _nowrapper=True, releasegil=False)

c_time = external('time', [rffi.VOIDP], rffi.TIME_T,
                  _nowrapper=True, releasegil=False)


def decode_timeval(t):
    return (float(rffi.getintfield(t, 'c_tv_sec')) +
            float(rffi.getintfield(t, 'c_tv_usec')) * 0.000001)


@replacement_for(time.time, sandboxed_name='ll_time.ll_time_time')
def floattime():
    # There are three ways to get the time:
    # (1) gettimeofday() -- resolution in microseconds
    # (2) ftime() -- resolution in milliseconds
    # (3) time() -- resolution in seconds
    # In all cases the return value is a float in seconds.
    # Since on some systems (e.g. SCO ODT 3.0) gettimeofday() may
    # fail, so we fall back on ftime() or time().
    # Note: clock resolution does not imply clock accuracy!

    void = lltype.nullptr(rffi.VOIDP.TO)
    result = -1.0
    if HAVE_GETTIMEOFDAY:
        with lltype.scoped_alloc(TIMEVAL) as t:
            errcode = -1
            if GETTIMEOFDAY_NO_TZ:
                errcode = c_gettimeofday(t)
            else:
                errcode = c_gettimeofday(t, void)

            if intmask(errcode) == 0:
                result = decode_timeval(t)
        if result != -1:
            return result
    if HAVE_FTIME:
        with lltype.scoped_alloc(TIMEB) as t:
            c_ftime(t)
            result = (float(intmask(t.c_time)) +
                      float(intmask(t.c_millitm)) * 0.001)
        return result
    else:
        return float(c_time(void))


if _WIN32:
    # hacking to avoid LARGE_INTEGER which is a union...
    A = lltype.FixedSizeArray(lltype.SignedLongLong, 1)
    QueryPerformanceCounter = external(
        'QueryPerformanceCounter', [lltype.Ptr(A)], lltype.Void,
        releasegil=False)
    QueryPerformanceFrequency = external(
        'QueryPerformanceFrequency', [lltype.Ptr(A)], rffi.INT,
        releasegil=False)
    class ClockState(object):
        divisor = 0.0
        counter_start = 0
    _clock_state = ClockState()
elif CLOCK_PROCESS_CPUTIME_ID is not None:
    # Linux and other POSIX systems with clock_gettime()
    class CConfigForClockGetTime:
        _compilation_info_ = ExternalCompilationInfo(
            includes=['time.h'],
            libraries=libraries
        )
        TIMESPEC = rffi_platform.Struct(
            'struct timespec', [
                ('tv_sec', rffi.LONG),
                ('tv_nsec', rffi.LONG)])

    cconfig = rffi_platform.configure(CConfigForClockGetTime)
    TIMESPEC = cconfig['TIMESPEC']
    c_clock_gettime = external('clock_gettime',
                               [lltype.Signed, lltype.Ptr(TIMESPEC)],
                               rffi.INT, releasegil=False)
else:
    c_getrusage = self.llexternal('getrusage', 
                                  [rffi.INT, lltype.Ptr(RUSAGE)],
                                  lltype.Void,
                                  releasegil=False)

@replacement_for(time.clock, sandboxed_name='ll_time.ll_time_clock')
def clock():
    if _WIN32:
        with lltype.static_alloc(A) as a:
            if _clock_state.divisor == 0.0:
                QueryPerformanceCounter(a)
                _clock_state.counter_start = a[0]
                QueryPerformanceFrequency(a)
                _clock_state.divisor = float(a[0])
            QueryPerformanceCounter(a)
            diff = a[0] - _clock_state.counter_start
        return float(diff) / _clock_state.divisor
    elif self.CLOCK_PROCESS_CPUTIME_ID is not None:
        with lltype.static_alloc(TIMESPEC) as a:
            c_clock_gettime(CLOCK_PROCESS_CPUTIME_ID, a)
            result = (float(rffi.getintfield(a, 'c_tv_sec')) +
                      float(rffi.getintfield(a, 'c_tv_nsec')) * 0.000000001)
        return result
    else:
        with lltype.static_alloc(RUSAGE) as a:
            c_getrusage(RUSAGE_SELF, a)
            result = (decode_timeval(a.c_ru_utime) +
                      decode_timeval(a.c_ru_stime))
        return result

    
