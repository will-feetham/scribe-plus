/*
 * audiotap: Scribe Plus's helper for call recording on a Mac.
 *
 *   audiotap version          what this Mac supports (JSON)
 *   audiotap list             every app that has audio open right now (JSON)
 *   audiotap watch            prints a JSON line whenever the set of apps using
 *                             the microphone or playing sound changes
 *   audiotap capture ...      records what one app (or the whole Mac) is
 *                             playing and writes it to stdout
 *
 * It uses two things Apple added to Core Audio:
 *   - audio "process objects" (macOS 14): which app has the mic open, which
 *     is playing sound. That is how Scribe Plus notices a call starting.
 *   - audio taps (macOS 14.2): a copy of what an app is playing, with no
 *     driver to install. That is the other side of the call. macOS asks for
 *     the "System Audio Recording" permission the first time.
 *
 * The app starts this helper as a child process, so macOS counts it as part
 * of Scribe Plus. It lives outside the app bundle, so updating it never
 * touches the permissions you granted.
 *
 * capture writes packets to stdout: "SPAT", sample rate (uint32), frame
 * count (uint32), then that many mono float32 samples, all little-endian.
 * Status and errors go to stderr as JSON lines. It stops when stdin closes
 * (the app quit), on SIGTERM, or after --seconds.
 *
 * Build (CI does this):
 *   clang -fobjc-arc -O2 -Wall -arch arm64 -arch x86_64 -mmacosx-version-min=12.0 \
 *       -framework Foundation -framework CoreAudio -o audiotap audiotap.m
 */
#import <Foundation/Foundation.h>
#import <CoreAudio/CoreAudio.h>
#import <CoreAudio/AudioHardwareTapping.h>
#import <CoreAudio/CATapDescription.h>
#import <objc/message.h>
#import <libproc.h>
#import <stdatomic.h>
#import <signal.h>
#import <unistd.h>

#define HELPER_VERSION 1
#define RING_FRAMES (1u << 20)          /* about 21 s at 48 kHz */
#define RING_MASK (RING_FRAMES - 1)
#define PACKET_FRAMES 4096

static _Atomic int g_stop = 0;

static float g_ring[RING_FRAMES];
static _Atomic uint64_t g_written = 0;   /* frames the audio thread has stored */
static _Atomic uint64_t g_callbacks = 0;
static _Atomic uint32_t g_layout = 0;    /* buffers << 16 | channels of the one we read */
static _Atomic uint32_t g_rate = 0;

/* ------------------------------------------------------------------ output */

static void emit(FILE *f, NSDictionary *obj) {
    NSData *d = [NSJSONSerialization dataWithJSONObject:obj
                                                options:NSJSONWritingSortedKeys error:nil];
    if (!d) return;
    fwrite(d.bytes, 1, d.length, f);
    fputc('\n', f);
    fflush(f);
}

static NSString *fourcc(OSStatus s) {
    char c[5] = { (char)(s >> 24), (char)(s >> 16), (char)(s >> 8), (char)s, 0 };
    for (int i = 0; i < 4; i++) if (c[i] < 32 || c[i] > 126) return [NSString stringWithFormat:@"%d", (int)s];
    return [NSString stringWithUTF8String:c];
}

static void fail(NSString *stage, OSStatus status, NSString *detail) {
    emit(stderr, @{ @"event": @"error", @"stage": stage, @"status": @((int)status),
                    @"code": fourcc(status), @"detail": detail ?: @"" });
}

static void onSignal(int sig) { (void)sig; atomic_store(&g_stop, 1); }

static void stopWhenStdinCloses(void) {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        char buf[64];
        for (;;) {
            ssize_t n = read(STDIN_FILENO, buf, sizeof buf);
            if (n > 0) continue;
            if (n < 0 && errno == EINTR) continue;
            atomic_store(&g_stop, 1);
            return;
        }
    });
}

/* --------------------------------------------------------- process objects */

static AudioObjectPropertyAddress addr(AudioObjectPropertySelector sel) {
    AudioObjectPropertyAddress a = { sel, kAudioObjectPropertyScopeGlobal,
                                     kAudioObjectPropertyElementMain };
    return a;
}

API_AVAILABLE(macos(14.2))
static NSArray<NSNumber *> *processObjects(void) {
    AudioObjectPropertyAddress a = addr(kAudioHardwarePropertyProcessObjectList);
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &a, 0, NULL, &size) != noErr
        || size < sizeof(AudioObjectID)) return @[];
    NSMutableData *buf = [NSMutableData dataWithLength:size];
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &size,
                                   buf.mutableBytes) != noErr) return @[];
    const AudioObjectID *ids = buf.bytes;
    NSMutableArray *out = [NSMutableArray array];
    for (UInt32 i = 0; i < size / sizeof(AudioObjectID); i++) [out addObject:@(ids[i])];
    return out;
}

static UInt32 uintProperty(AudioObjectID obj, AudioObjectPropertySelector sel) {
    AudioObjectPropertyAddress a = addr(sel);
    UInt32 value = 0, size = sizeof value;
    if (AudioObjectGetPropertyData(obj, &a, 0, NULL, &size, &value) != noErr) return 0;
    return value;
}

static NSString *stringProperty(AudioObjectID obj, AudioObjectPropertySelector sel) {
    AudioObjectPropertyAddress a = addr(sel);
    CFStringRef value = NULL;
    UInt32 size = sizeof value;
    if (AudioObjectGetPropertyData(obj, &a, 0, NULL, &size, &value) != noErr || !value) return @"";
    return (__bridge_transfer NSString *)value;
}

static NSString *pathOfPid(pid_t pid) {
    char buf[PROC_PIDPATHINFO_MAXSIZE];
    int n = proc_pidpath(pid, buf, sizeof buf);
    return n > 0 ? ([[NSString alloc] initWithBytes:buf length:(NSUInteger)n
                                           encoding:NSUTF8StringEncoding] ?: @"") : @"";
}

/* One entry per app process that has audio open: who it is, and whether it
   has the microphone open ("input") or is playing sound ("output"). */
API_AVAILABLE(macos(14.2))
static NSArray<NSDictionary *> *snapshot(BOOL activeOnly) {
    NSMutableArray *out = [NSMutableArray array];
    for (NSNumber *n in processObjects()) {
        AudioObjectID obj = n.unsignedIntValue;
        BOOL input = uintProperty(obj, kAudioProcessPropertyIsRunningInput) != 0;
        BOOL output = uintProperty(obj, kAudioProcessPropertyIsRunningOutput) != 0;
        if (activeOnly && !input && !output) continue;
        pid_t pid = (pid_t)uintProperty(obj, kAudioProcessPropertyPID);
        [out addObject:@{ @"object": n, @"pid": @(pid),
                          @"bundle": stringProperty(obj, kAudioProcessPropertyBundleID),
                          @"path": pathOfPid(pid),
                          @"input": @(input), @"output": @(output) }];
    }
    [out sortUsingComparator:^NSComparisonResult(NSDictionary *a, NSDictionary *b) {
        return [a[@"pid"] compare:b[@"pid"]];
    }];
    return out;
}

static BOOL hasPrefixCI(NSString *s, NSString *prefix) {
    return prefix.length && s.length >= prefix.length
        && [s compare:prefix options:NSCaseInsensitiveSearch
                range:NSMakeRange(0, prefix.length)] == NSOrderedSame;
}

API_AVAILABLE(macos(14.2))
static NSArray<NSDictionary *> *matching(NSArray<NSString *> *bundles, NSArray<NSString *> *paths) {
    NSMutableArray *out = [NSMutableArray array];
    for (NSDictionary *p in snapshot(NO)) {
        BOOL hit = NO;
        for (NSString *b in bundles) if (hasPrefixCI(p[@"bundle"], b)) hit = YES;
        for (NSString *q in paths) if (hasPrefixCI(p[@"path"], q)) hit = YES;
        if (hit) [out addObject:p];
    }
    return out;
}

/* -------------------------------------------------------------------- watch */

API_AVAILABLE(macos(14.2))
static int runWatch(double interval) {
    stopWhenStdinCloses();
    NSString *last = nil;
    while (!atomic_load(&g_stop)) {
        @autoreleasepool {
            NSArray *procs = snapshot(YES);
            NSData *d = [NSJSONSerialization dataWithJSONObject:procs
                                                        options:NSJSONWritingSortedKeys error:nil];
            NSString *now = d ? [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] : @"";
            if (![now isEqualToString:last ?: @"\x01"]) {
                last = now;
                emit(stdout, @{ @"event": @"procs", @"t": @([NSDate date].timeIntervalSince1970),
                                @"procs": procs });
            }
        }
        usleep((useconds_t)(interval * 1e6));
    }
    return 0;
}

/* ------------------------------------------------------------------ capture */

typedef struct { AudioObjectID tap, device; AudioDeviceIOProcID proc; } Rig;

API_AVAILABLE(macos(14.2))
static void setBoolIfPossible(id obj, NSString *setter, BOOL value) {
    SEL sel = NSSelectorFromString(setter);
    if ([obj respondsToSelector:sel]) ((void (*)(id, SEL, BOOL))objc_msgSend)(obj, sel, value);
}

API_AVAILABLE(macos(14.2))
static AudioStreamBasicDescription tapFormat(AudioObjectID tap) {
    AudioObjectPropertyAddress a = addr(kAudioTapPropertyFormat);
    AudioStreamBasicDescription f = {0};
    UInt32 size = sizeof f;
    AudioObjectGetPropertyData(tap, &a, 0, NULL, &size, &f);
    return f;
}

static NSString *deviceUID(AudioObjectID dev) {
    return stringProperty(dev, kAudioDevicePropertyDeviceUID);
}

/* The rate the audio actually arrives at. It is read from the device the
   callbacks run on: next to an output device, the tap's audio is converted
   to that device's rate, whatever the tap's own format says. */
API_AVAILABLE(macos(14.2))
static uint32_t readingRate(AudioObjectID device, AudioObjectID tap) {
    Float64 rate = 0;
    if (device != kAudioObjectUnknown) {
        AudioObjectPropertyAddress a = addr(kAudioDevicePropertyNominalSampleRate);
        UInt32 size = sizeof rate;
        if (AudioObjectGetPropertyData(device, &a, 0, NULL, &size, &rate) != noErr) rate = 0;
    }
    if (rate <= 0) rate = tapFormat(tap).mSampleRate;
    return rate > 0 ? (uint32_t)(rate + 0.5) : 48000;
}

static AudioObjectID defaultOutputDevice(void) {
    AudioObjectPropertyAddress a = addr(kAudioHardwarePropertyDefaultSystemOutputDevice);
    AudioObjectID dev = kAudioObjectUnknown;
    UInt32 size = sizeof dev;
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &size, &dev) != noErr
        || dev == kAudioObjectUnknown) {
        a = addr(kAudioHardwarePropertyDefaultOutputDevice);
        size = sizeof dev;
        AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &size, &dev);
    }
    return dev;
}

API_AVAILABLE(macos(14.2))
static void teardown(Rig *r) {
    if (r->device != kAudioObjectUnknown) {
        if (r->proc) {
            AudioDeviceStop(r->device, r->proc);
            AudioDeviceDestroyIOProcID(r->device, r->proc);
        }
        AudioHardwareDestroyAggregateDevice(r->device);
    }
    r->device = kAudioObjectUnknown;
    r->proc = NULL;
}

/* Wraps the tap in a private "aggregate device" (the only way to read a tap)
   and starts reading. style 0: the tap alone. style 1: the tap next to the
   current output device, which some Macs need for a clock. Returns YES once
   audio is arriving (silence counts: it arrives as zeros). With strict NO,
   callbacks that carry no audio yet are enough. */
API_AVAILABLE(macos(14.2))
static BOOL startReading(Rig *r, NSString *tapUID, int style, BOOL strict) {
    NSString *uid = [NSUUID UUID].UUIDString;
    NSMutableDictionary *desc = [@{
        @kAudioAggregateDeviceNameKey: @"Scribe Plus call recording",
        @kAudioAggregateDeviceUIDKey: uid,
        @kAudioAggregateDeviceIsPrivateKey: @YES,
        @kAudioAggregateDeviceIsStackedKey: @NO,
    } mutableCopy];
    NSString *label = style == 0 ? @"tap only" : @"tap with output device";
    if (style == 0) {
        desc[@kAudioAggregateDeviceSubDeviceListKey] = @[];
    } else {
        NSString *out = deviceUID(defaultOutputDevice());
        if (!out.length) { fail(@"output_device", 0, @"no default output device"); return NO; }
        desc[@kAudioAggregateDeviceMainSubDeviceKey] = out;
        desc[@kAudioAggregateDeviceSubDeviceListKey] = @[ @{ @kAudioSubDeviceUIDKey: out } ];
        desc[@kAudioAggregateDeviceTapAutoStartKey] = @YES;
        desc[@kAudioAggregateDeviceTapListKey] =
            @[ @{ @kAudioSubTapUIDKey: tapUID, @kAudioSubTapDriftCompensationKey: @YES } ];
    }
    OSStatus err = AudioHardwareCreateAggregateDevice((__bridge CFDictionaryRef)desc, &r->device);
    if (err != noErr) { fail(@"create_device", err, label); r->device = kAudioObjectUnknown; return NO; }

    if (style == 0) {
        AudioObjectPropertyAddress a = addr(kAudioAggregateDevicePropertyTapList);
        /* Held in a variable for the whole block, so the list is still alive
           when Core Audio reads it. */
        NS_VALID_UNTIL_END_OF_SCOPE NSArray *tapList = @[ tapUID ];
        CFArrayRef taps = (__bridge CFArrayRef)tapList;
        err = AudioObjectSetPropertyData(r->device, &a, 0, NULL, sizeof taps, &taps);
        if (err != noErr) { fail(@"add_tap", err, label); teardown(r); return NO; }
        usleep(150000);  /* the device rebuilds its streams after the tap is added */
    }

    atomic_store(&g_callbacks, 0);
    atomic_store(&g_layout, 0);
    uint64_t before = atomic_load(&g_written);
    err = AudioDeviceCreateIOProcIDWithBlock(&r->proc, r->device, NULL,
        ^(const AudioTimeStamp *now, const AudioBufferList *in, const AudioTimeStamp *inTime,
          AudioBufferList *out, const AudioTimeStamp *outTime) {
            atomic_fetch_add(&g_callbacks, 1);
            if (!in || in->mNumberBuffers == 0) return;
            /* The tap's stream comes after any streams of the output device. */
            const AudioBuffer *b = &in->mBuffers[in->mNumberBuffers - 1];
            UInt32 ch = b->mNumberChannels ? b->mNumberChannels : 1;
            const Float32 *p = (const Float32 *)b->mData;
            if (!p) return;
            UInt32 frames = b->mDataByteSize / (UInt32)(sizeof(Float32) * ch);
            atomic_store(&g_layout, (in->mNumberBuffers << 16) | ch);
            uint64_t w = atomic_load_explicit(&g_written, memory_order_relaxed);
            for (UInt32 i = 0; i < frames; i++) {
                float s = 0;
                for (UInt32 c = 0; c < ch; c++) s += p[i * ch + c];
                g_ring[(w + i) & RING_MASK] = s / (float)ch;
            }
            atomic_store_explicit(&g_written, w + frames, memory_order_release);
        });
    if (err != noErr) { fail(@"create_ioproc", err, label); r->proc = NULL; teardown(r); return NO; }
    err = AudioDeviceStart(r->device, r->proc);
    if (err != noErr) { fail(@"start", err, label); teardown(r); return NO; }

    for (int i = 0; i < 100 && !atomic_load(&g_stop); i++) {   /* up to 2 s */
        if (atomic_load(&g_written) > before) return YES;
        if (!strict && atomic_load(&g_callbacks) > 2) return YES;
        usleep(20000);
    }
    if (strict) {
        emit(stderr, @{ @"event": @"no_audio", @"device_style": label,
                        @"callbacks": @(atomic_load(&g_callbacks)) });
    } else {
        fail(@"no_audio_callbacks", 0, label);
    }
    teardown(r);
    return NO;
}

API_AVAILABLE(macos(14.2))
static NSArray<NSNumber *> *objectIDs(NSArray<NSDictionary *> *procs) {
    NSMutableArray *ids = [NSMutableArray array];
    for (NSDictionary *p in procs) [ids addObject:p[@"object"]];
    return ids;
}

API_AVAILABLE(macos(14.2))
static int runCapture(NSArray<NSString *> *bundles, NSArray<NSString *> *paths,
                      BOOL systemWide, BOOL appOnly, double seconds, BOOL useStdin) {
    signal(SIGPIPE, SIG_IGN);
    if (useStdin) stopWhenStdinCloses();

    NSArray<NSDictionary *> *matched = systemWide ? @[] : matching(bundles, paths);
    if (!systemWide && matched.count == 0 && appOnly) {
        fail(@"match", 0, @"the app has no audio open");
        return 3;
    }
    BOOL app = matched.count > 0;
    CATapDescription *desc = app
        ? [[CATapDescription alloc] initMonoMixdownOfProcesses:objectIDs(matched)]
        : [[CATapDescription alloc] initMonoGlobalTapButExcludeProcesses:@[]];
    desc.name = @"Scribe Plus call recording";
    desc.UUID = [NSUUID UUID];
    setBoolIfPossible(desc, @"setPrivateTap:", YES);
    setBoolIfPossible(desc, @"setPrivate:", YES);

    Rig rig = { kAudioObjectUnknown, kAudioObjectUnknown, NULL };
    OSStatus err = AudioHardwareCreateProcessTap(desc, &rig.tap);
    if (err != noErr || rig.tap == kAudioObjectUnknown) {
        fail(@"create_tap", err, app ? @"app" : @"system");
        return 4;
    }
    NSString *tapUID = stringProperty(rig.tap, kAudioTapPropertyUID);
    if (!tapUID.length) tapUID = desc.UUID.UUIDString;

    /* First either way of reading that delivers audio; failing that, either
       way that at least runs (the audio may follow, and the loop below keeps
       trying). */
    int style = -1;
    BOOL flowing = YES;
    for (int pass = 0; pass < 2 && style < 0; pass++) {
        for (int s = 0; s < 2 && style < 0 && !atomic_load(&g_stop); s++) {
            if (startReading(&rig, tapUID, s, pass == 0)) { style = s; flowing = pass == 0; }
        }
    }
    if (style < 0) {
        AudioHardwareDestroyProcessTap(rig.tap);
        return atomic_load(&g_stop) ? 0 : 5;
    }

    AudioStreamBasicDescription fmt = tapFormat(rig.tap);
    atomic_store(&g_rate, readingRate(rig.device, rig.tap));
    uint32_t layout = atomic_load(&g_layout);
    emit(stderr, @{ @"event": @"started", @"mode": app ? @"app" : @"system",
                    @"matched": matched, @"rate": @(atomic_load(&g_rate)),
                    @"tap_rate": @((uint32_t)fmt.mSampleRate), @"audio_flowing": @(flowing),
                    @"format_channels": @(fmt.mChannelsPerFrame),
                    @"float": @((fmt.mFormatFlags & kAudioFormatFlagIsFloat) != 0),
                    @"device_style": style == 0 ? @"tap only" : @"tap with output device",
                    @"buffers": @(layout >> 16), @"channels_read": @(layout & 0xffff) });

    uint64_t readPos = atomic_load(&g_written);   /* skip what arrived while starting */
    uint64_t lastWritten = readPos;
    uint64_t sent = 0;
    float peak = 0;
    double sumsq = 0;
    uint64_t levelFrames = 0;
    NSDate *begun = [NSDate date];
    NSDate *lastLevel = begun, *lastScan = begun, *lastFormat = begun, *lastAudio = begun;
    static float chunk[PACKET_FRAMES];
    BOOL broken = NO;
    int restarts = 0;

    while (!atomic_load(&g_stop) && !broken) {
        @autoreleasepool {
            uint64_t w = atomic_load_explicit(&g_written, memory_order_acquire);
            if (w - readPos > RING_FRAMES) readPos = w - RING_FRAMES;   /* fell behind: drop the oldest */
            while (readPos < w && !broken) {
                uint32_t n = (uint32_t)MIN((uint64_t)PACKET_FRAMES, w - readPos);
                for (uint32_t i = 0; i < n; i++) {
                    float s = g_ring[(readPos + i) & RING_MASK];
                    chunk[i] = s;
                    float a = s < 0 ? -s : s;
                    if (a > peak) peak = a;
                    sumsq += (double)s * s;
                }
                uint32_t header[3] = { 0x54415053u /* "SPAT" */, atomic_load(&g_rate), n };
                if (fwrite(header, sizeof header, 1, stdout) != 1
                    || fwrite(chunk, sizeof(float), n, stdout) != n) broken = YES;
                readPos += n;
                sent += n;
                levelFrames += n;
            }
            if (fflush(stdout) != 0) broken = YES;

            NSDate *now = [NSDate date];
            /* The rate changes mid-call when the sound moves to another device,
               or when a headset switches to its call mode. */
            if ([now timeIntervalSinceDate:lastFormat] >= 0.25) {
                lastFormat = now;
                uint32_t rate = readingRate(rig.device, rig.tap);
                if (rate > 0 && rate != atomic_load(&g_rate)) {
                    atomic_store(&g_rate, rate);
                    emit(stderr, @{ @"event": @"rate", @"rate": @(rate) });
                }
            }
            /* Audio arrives all the time, silence included. If it stops, the
               device under the tap went away: read it again from scratch. */
            if (w != lastWritten) {
                lastWritten = w;
                lastAudio = now;
            } else if ([now timeIntervalSinceDate:lastAudio] >= 3.0 && restarts < 5) {
                restarts++;
                emit(stderr, @{ @"event": @"restart", @"attempt": @(restarts) });
                teardown(&rig);
                BOOL again = startReading(&rig, tapUID, style, YES)
                    || startReading(&rig, tapUID, 1 - style, YES)
                    || startReading(&rig, tapUID, style, NO);
                if (!again) { fail(@"restart", 0, @"the audio did not come back"); break; }
                readPos = lastWritten = atomic_load(&g_written);
                lastAudio = [NSDate date];
            }
            if ([now timeIntervalSinceDate:lastLevel] >= 2.0) {
                emit(stderr, @{ @"event": @"level", @"peak": @(peak),
                                @"rms": @(levelFrames ? sqrt(sumsq / (double)levelFrames) : 0.0),
                                @"frames": @(sent), @"rate": @(atomic_load(&g_rate)),
                                @"callbacks": @(atomic_load(&g_callbacks)) });
                peak = 0; sumsq = 0; levelFrames = 0; lastLevel = now;
            }
            /* Apps start and stop helper processes; keep the tap pointed at all of them. */
            if (app && [now timeIntervalSinceDate:lastScan] >= 2.0) {
                lastScan = now;
                NSArray<NSDictionary *> *again = matching(bundles, paths);
                NSArray<NSNumber *> *ids = objectIDs(again);
                if (ids.count && ![[NSSet setWithArray:ids] isEqualToSet:[NSSet setWithArray:objectIDs(matched)]]) {
                    matched = again;
                    desc.processes = ids;
                    AudioObjectPropertyAddress a = addr(kAudioTapPropertyDescription);
                    CFTypeRef ref = (__bridge CFTypeRef)desc;
                    OSStatus e = AudioObjectSetPropertyData(rig.tap, &a, 0, NULL, sizeof ref, &ref);
                    emit(stderr, @{ @"event": @"retarget", @"status": @((int)e), @"matched": matched });
                }
            }
            if (seconds > 0 && [now timeIntervalSinceDate:begun] >= seconds) break;
        }
        usleep(20000);
    }

    teardown(&rig);
    AudioHardwareDestroyProcessTap(rig.tap);
    emit(stderr, @{ @"event": @"stopped", @"frames": @(sent) });
    return 0;
}

/* --------------------------------------------------------------------- main */

int main(int argc, const char **argv) {
    @autoreleasepool {
        signal(SIGTERM, onSignal);
        signal(SIGINT, onSignal);
        NSMutableArray<NSString *> *args = [NSMutableArray array];
        for (int i = 1; i < argc; i++) [args addObject:@(argv[i])];
        NSString *cmd = args.count ? args[0] : @"version";

        BOOL supported = NO;
        if (@available(macOS 14.2, *)) supported = YES;
        NSOperatingSystemVersion v = NSProcessInfo.processInfo.operatingSystemVersion;
        NSString *os = [NSString stringWithFormat:@"%ld.%ld.%ld", (long)v.majorVersion,
                        (long)v.minorVersion, (long)v.patchVersion];

        if ([cmd isEqualToString:@"version"]) {
            emit(stdout, @{ @"helper": @HELPER_VERSION, @"macos": os, @"supported": @(supported) });
            return 0;
        }
        if (!supported) {
            fail(@"macos", 0, [NSString stringWithFormat:
                @"call recording needs macOS 14.2 or newer (this Mac has %@)", os]);
            return 2;
        }
        if (@available(macOS 14.2, *)) {
            if ([cmd isEqualToString:@"list"]) {
                emit(stdout, @{ @"event": @"procs", @"procs": snapshot(NO) });
                return 0;
            }
            NSMutableArray<NSString *> *bundles = [NSMutableArray array];
            NSMutableArray<NSString *> *paths = [NSMutableArray array];
            BOOL systemWide = NO, appOnly = NO, useStdin = YES;
            double seconds = 0, interval = 0.5;
            for (NSUInteger i = 1; i < args.count; i++) {
                NSString *a = args[i];
                NSString *next = i + 1 < args.count ? args[i + 1] : nil;
                if ([a isEqualToString:@"--match"] && next) { [bundles addObject:next]; i++; }
                else if ([a isEqualToString:@"--path"] && next) { [paths addObject:next]; i++; }
                else if ([a isEqualToString:@"--seconds"] && next) { seconds = next.doubleValue; i++; }
                else if ([a isEqualToString:@"--interval"] && next) { interval = MAX(0.1, next.doubleValue); i++; }
                else if ([a isEqualToString:@"--system"]) systemWide = YES;
                else if ([a isEqualToString:@"--app-only"]) appOnly = YES;
                else if ([a isEqualToString:@"--no-stdin"]) useStdin = NO;
                else { fail(@"arguments", 0, a); return 64; }
            }
            if ([cmd isEqualToString:@"watch"]) return runWatch(interval);
            if ([cmd isEqualToString:@"capture"])
                return runCapture(bundles, paths, systemWide, appOnly, seconds, useStdin);
        }
        fail(@"arguments", 0, cmd);
        return 64;
    }
}
