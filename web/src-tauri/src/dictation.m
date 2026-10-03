#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>
#import <Speech/Speech.h>

typedef void (*RCPDictationCallback)(const char *session_id,
                                     const char *kind,
                                     const char *text,
                                     int is_final,
                                     const char *state,
                                     const char *error);

#ifdef RCP_SPEECH_ANALYZER
extern int rcp_analyzer_start(const char *, RCPDictationCallback);
extern void rcp_analyzer_stop(int finish);
static void analyzerEvent(const char *, const char *, const char *, int, const char *, const char *);
#endif

@interface RCPDictationController : NSObject

@property(nonatomic, copy, nullable) NSString *sessionID;
@property(nonatomic) RCPDictationCallback callback;
@property(nonatomic) BOOL usingAnalyzer;
@property(nonatomic) BOOL finishing;
@property(nonatomic, strong, nullable) AVAudioEngine *audioEngine;
@property(nonatomic, strong, nullable) SFSpeechRecognizer *recognizer;
@property(nonatomic, strong, nullable) SFSpeechAudioBufferRecognitionRequest *request;
@property(nonatomic, strong, nullable) SFSpeechRecognitionTask *task;

+ (instancetype)shared;
- (void)startWithSessionID:(NSString *)sessionID callback:(RCPDictationCallback)callback;
- (BOOL)stopSessionID:(NSString *)sessionID finish:(BOOL)finish;
- (void)finishActive;
- (void)beginRecognitionForSession:(NSString *)sessionID;
- (BOOL)sessionIsActive:(NSString *)sessionID;
- (void)stopActive;

@end

@implementation RCPDictationController

+ (instancetype)shared {
    static RCPDictationController *controller;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
      controller = [[RCPDictationController alloc] init];
    });
    return controller;
}

- (void)startWithSessionID:(NSString *)sessionID callback:(RCPDictationCallback)callback {
    dispatch_async(dispatch_get_main_queue(), ^{
      [self stopActive];
      self.sessionID = sessionID;
      self.callback = callback;
      self.finishing = NO;

      [SFSpeechRecognizer requestAuthorization:^(SFSpeechRecognizerAuthorizationStatus status) {
        dispatch_async(dispatch_get_main_queue(), ^{
          if (![self sessionIsActive:sessionID]) {
              return;
          }
          if (status != SFSpeechRecognizerAuthorizationStatusAuthorized) {
              [self failSession:sessionID message:@"Speech recognition permission was not granted."];
              return;
          }
          [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio
                                  completionHandler:^(BOOL granted) {
            dispatch_async(dispatch_get_main_queue(), ^{
              if (![self sessionIsActive:sessionID]) {
                  return;
              }
              if (!granted) {
                  [self failSession:sessionID message:@"Microphone permission was not granted."];
                  return;
              }
#ifdef RCP_SPEECH_ANALYZER
              self.usingAnalyzer = rcp_analyzer_start(sessionID.UTF8String, analyzerEvent) != 0;
              if (self.usingAnalyzer) {
                  return;
              }
#endif
              [self beginRecognitionForSession:sessionID];
            });
          }];
        });
      }];
    });
}

- (BOOL)stopSessionID:(NSString *)sessionID finish:(BOOL)finish {
    if ([NSThread isMainThread]) {
        if (![self sessionIsActive:sessionID]) {
            return NO;
        }
        finish ? [self finishActive] : [self stopActive];
        return YES;
    }

    __block BOOL stopped = NO;
    dispatch_sync(dispatch_get_main_queue(), ^{
      if ([self sessionIsActive:sessionID]) {
          finish ? [self finishActive] : [self stopActive];
          stopped = YES;
      }
    });
    return stopped;
}

- (void)finishActive {
    if (self.sessionID == nil || self.finishing) {
        return;
    }
    self.finishing = YES;
#ifdef RCP_SPEECH_ANALYZER
    if (self.usingAnalyzer) {
        rcp_analyzer_stop(1);
        return;
    }
#endif
    if (self.request == nil) {
        if (self.callback != NULL) {
            self.callback(self.sessionID.UTF8String, "result", "", 1, "", "");
        }
        [self stopActive];
        return;
    }
    // Keep the result handler alive until the recognizer delivers its final text.
    [self.audioEngine stop];
    [self.request endAudio];
    [self.task finish];
}

- (void)stopActive {
    if (![NSThread isMainThread]) {
        dispatch_async(dispatch_get_main_queue(), ^{
          [self stopActive];
        });
        return;
    }
    NSString *sessionID = self.sessionID;
    if (sessionID == nil) {
        return;
    }
    RCPDictationCallback callback = self.callback;
    [self tearDownRecognition];
    self.sessionID = nil;
    self.callback = NULL;
    if (callback != NULL) {
        callback(sessionID.UTF8String, "state", "", 0, "stopped", "");
    }
}

- (void)beginRecognitionForSession:(NSString *)sessionID {
    SFSpeechRecognizer *recognizer = [[SFSpeechRecognizer alloc] init];
    if (recognizer == nil || !recognizer.available) {
        [self failSession:sessionID message:@"Apple speech recognition is currently unavailable."];
        return;
    }

    SFSpeechAudioBufferRecognitionRequest *request =
        [[SFSpeechAudioBufferRecognitionRequest alloc] init];
    request.shouldReportPartialResults = YES;
    request.requiresOnDeviceRecognition = NO;
    request.taskHint = SFSpeechRecognitionTaskHintDictation;
    request.addsPunctuation = YES;

    AVAudioEngine *audioEngine = [[AVAudioEngine alloc] init];
    AVAudioInputNode *inputNode = audioEngine.inputNode;
    AVAudioFormat *format = [inputNode outputFormatForBus:0];
    if (format.channelCount == 0 || format.sampleRate == 0) {
        [self failSession:sessionID message:@"No working microphone input is available."];
        return;
    }

    self.recognizer = recognizer;
    self.request = request;
    self.audioEngine = audioEngine;

    __weak RCPDictationController *weakSelf = self;
    self.task = [recognizer recognitionTaskWithRequest:request
                                         resultHandler:^(SFSpeechRecognitionResult *result,
                                                         NSError *error) {
      dispatch_async(dispatch_get_main_queue(), ^{
        RCPDictationController *strongSelf = weakSelf;
        if (strongSelf == nil || ![strongSelf sessionIsActive:sessionID]) {
            return;
        }
        if (result != nil && strongSelf.callback != NULL) {
            NSString *text = result.bestTranscription.formattedString ?: @"";
            strongSelf.callback(sessionID.UTF8String,
                                "result",
                                text.UTF8String,
                                result.final ? 1 : 0,
                                "",
                                "");
        }
        if (result.final) {
            [strongSelf stopActive];
        } else if (error != nil) {
            NSString *message = error.localizedDescription ?: @"Speech recognition failed.";
            [strongSelf failSession:sessionID message:message];
        }
      });
    }];

    [inputNode installTapOnBus:0
                    bufferSize:1024
                        format:format
                         block:^(AVAudioPCMBuffer *buffer, AVAudioTime *when) {
      (void)when;
      [request appendAudioPCMBuffer:buffer];
    }];

    [audioEngine prepare];
    NSError *startError = nil;
    if (![audioEngine startAndReturnError:&startError]) {
        [self failSession:sessionID
                  message:startError.localizedDescription ?: @"Could not start microphone capture."];
        return;
    }

    [self emitState:@"recording" error:nil forSession:sessionID];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(55 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
      RCPDictationController *strongSelf = weakSelf;
      if (strongSelf != nil && [strongSelf sessionIsActive:sessionID]) {
          [strongSelf finishActive];
      }
    });
}

- (void)failSession:(NSString *)sessionID message:(NSString *)message {
    if (![self sessionIsActive:sessionID]) {
        return;
    }
    RCPDictationCallback callback = self.callback;
    [self tearDownRecognition];
    self.sessionID = nil;
    self.callback = NULL;
    if (callback != NULL) {
        callback(sessionID.UTF8String, "state", "", 0, "error", message.UTF8String);
    }
}

- (void)emitState:(NSString *)state error:(nullable NSString *)error forSession:(NSString *)sessionID {
    if (self.callback == NULL || ![self sessionIsActive:sessionID]) {
        return;
    }
    self.callback(sessionID.UTF8String,
                  "state",
                  "",
                  0,
                  state.UTF8String,
                  error == nil ? "" : error.UTF8String);
}

- (BOOL)sessionIsActive:(NSString *)sessionID {
    return self.sessionID != nil && [self.sessionID isEqualToString:sessionID];
}

- (void)tearDownRecognition {
#ifdef RCP_SPEECH_ANALYZER
    if (self.usingAnalyzer) {
        rcp_analyzer_stop(0);
        self.usingAnalyzer = NO;
    }
#endif
    if (self.audioEngine != nil) {
        [self.audioEngine stop];
        [self.audioEngine.inputNode removeTapOnBus:0];
    }
    [self.request endAudio];
    [self.task cancel];
    self.task = nil;
    self.request = nil;
    self.recognizer = nil;
    self.audioEngine = nil;
}

@end

#ifdef RCP_SPEECH_ANALYZER
static void analyzerEvent(const char *session, const char *kind, const char *text,
                          int final, const char *state, const char *error) {
    RCPDictationController *controller = [RCPDictationController shared];
    NSString *sessionID = [NSString stringWithUTF8String:session];
    if (![controller sessionIsActive:sessionID]) {
        return;
    }
    if (strcmp(kind, "fallback") == 0) {
        controller.usingAnalyzer = NO;
        [controller beginRecognitionForSession:sessionID];
        return;
    }
    if (controller.callback != NULL) {
        controller.callback(session, kind, text, final, state, error);
    }
    if (strcmp(kind, "state") == 0 &&
        (strcmp(state, "stopped") == 0 || strcmp(state, "error") == 0)) {
        controller.usingAnalyzer = NO;
        controller.sessionID = nil;
        controller.callback = NULL;
    }
}
#endif

int rcp_dictation_start(const char *session_id, RCPDictationCallback callback) {
    if (session_id == NULL || callback == NULL) {
        return 1;
    }
    NSString *sessionID = [[NSString alloc] initWithUTF8String:session_id];
    if (sessionID == nil || sessionID.length == 0) {
        return 1;
    }
    [[RCPDictationController shared] startWithSessionID:sessionID callback:callback];
    return 0;
}

int rcp_dictation_stop(const char *session_id, int finish) {
    if (session_id == NULL) {
        return 1;
    }
    NSString *sessionID = [[NSString alloc] initWithUTF8String:session_id];
    if (sessionID == nil) {
        return 1;
    }
    return [[RCPDictationController shared] stopSessionID:sessionID finish:finish != 0] ? 0 : 1;
}

void rcp_dictation_stop_active(void) {
    [[RCPDictationController shared] stopActive];
}
