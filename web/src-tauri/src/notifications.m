#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>

// kind is "click" (text = deep link), "posted" (text = empty), or "error"
// (text = message). The id is the stable notification id in every case.
typedef void (*RCPNotificationCallback)(const char *kind, const char *notification_id,
                                        const char *text);

static NSString *const RCPNotificationLinkKey = @"rcp_link";

@interface RCPNotificationController : NSObject <UNUserNotificationCenterDelegate>

@property(nonatomic) RCPNotificationCallback callback;

+ (instancetype)shared;

@end

@implementation RCPNotificationController

+ (instancetype)shared {
    static RCPNotificationController *controller;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
      controller = [[RCPNotificationController alloc] init];
    });
    return controller;
}

- (void)emit:(NSString *)kind notificationID:(NSString *)notificationID text:(NSString *)text {
    RCPNotificationCallback callback = self.callback;
    if (callback == NULL) {
        return;
    }
    callback(kind.UTF8String, notificationID.UTF8String, (text ?: @"").UTF8String);
}

// Show banners while the app is frontmost too; the app never shows its own.
- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completionHandler {
    completionHandler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList |
                      UNNotificationPresentationOptionSound);
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
    didReceiveNotificationResponse:(UNNotificationResponse *)response
             withCompletionHandler:(void (^)(void))completionHandler {
    if ([response.actionIdentifier isEqualToString:UNNotificationDefaultActionIdentifier]) {
        UNNotificationRequest *request = response.notification.request;
        id link = request.content.userInfo[RCPNotificationLinkKey];
        [self emit:@"click"
            notificationID:request.identifier
                      text:[link isKindOfClass:[NSString class]] ? link : @""];
    }
    completionHandler();
}

@end

// Must run before the app finishes launching, so a click that launches the
// app is delivered to the delegate.
void rcp_notifications_install(RCPNotificationCallback callback) {
    RCPNotificationController *controller = [RCPNotificationController shared];
    controller.callback = callback;
    [UNUserNotificationCenter currentNotificationCenter].delegate = controller;
}

// Asks for permission on the first call; macOS remembers the answer.
void rcp_notifications_post(const char *notification_id, const char *title, const char *body,
                            const char *link) {
    NSString *identifier = [NSString stringWithUTF8String:notification_id];
    NSString *titleText = [NSString stringWithUTF8String:title];
    NSString *bodyText = [NSString stringWithUTF8String:body];
    NSString *linkText = [NSString stringWithUTF8String:link];
    UNUserNotificationCenter *center = [UNUserNotificationCenter currentNotificationCenter];
    RCPNotificationController *controller = [RCPNotificationController shared];
    UNAuthorizationOptions options = UNAuthorizationOptionAlert | UNAuthorizationOptionSound;
    [center requestAuthorizationWithOptions:options
                          completionHandler:^(BOOL granted, NSError *_Nullable error) {
      if (!granted) {
          [controller emit:@"error"
              notificationID:identifier
                        text:error.localizedDescription ?: @"Notifications are turned off for RCP."];
          return;
      }
      UNMutableNotificationContent *content = [[UNMutableNotificationContent alloc] init];
      content.title = titleText;
      content.body = bodyText;
      content.sound = [UNNotificationSound defaultSound];
      content.userInfo = @{RCPNotificationLinkKey : linkText};
      // Reusing an identifier replaces the delivered notification.
      UNNotificationRequest *request = [UNNotificationRequest requestWithIdentifier:identifier
                                                                            content:content
                                                                            trigger:nil];
      [center addNotificationRequest:request
               withCompletionHandler:^(NSError *_Nullable addError) {
                 if (addError != nil) {
                     [controller emit:@"error"
                         notificationID:identifier
                                   text:addError.localizedDescription];
                 } else {
                     [controller emit:@"posted" notificationID:identifier text:@""];
                 }
               }];
    }];
}
