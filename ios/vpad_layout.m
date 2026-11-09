#import "vpad_layout.h"
#include <math.h>

NSString *const S2AVPadLayoutDefaultIdentifier = @"default";
NSString *const S2AVPadLayoutMineIdentifier = @"mine";
// 纯触屏：没有虚拟按键，也就没有可编辑的几何，因此不落文件、只记在选择记录里。
NSString *const S2AVPadLayoutTouchIdentifier = @"touch";

CGFloat S2AVPadKeyOpacity(CGFloat idle, BOOL active) {
    return active ? MAX(0.75, idle) : idle;
}

// Foundation's JSON reader can round two related decimal fields in opposite
// directions (observed for the iPad default's 54/687 and 27/687). This is far
// below a screen pixel; do not reject a valid round trip on a cross-field edge.
static const double geometryTolerance = 1e-12;

static NSError *layout_error(NSString *code, NSString *message) {
    return [NSError errorWithDomain:@"S2Arm.VPadLayout" code:1
                          userInfo:@{NSLocalizedDescriptionKey: [NSString stringWithFormat:@"%@：%@", code, message]}];
}
static BOOL fail(NSError **error, NSString *code, NSString *message) {
    if (error) *error = layout_error(code, message);
    return NO;
}
static BOOL fields(id value, NSArray *keys) {
    return [value isKindOfClass:NSDictionary.class] &&
        [[NSSet setWithArray:[value allKeys]] isEqualToSet:[NSSet setWithArray:keys]];
}
static BOOL number(id value, double low, double high) {
    return [value isKindOfClass:NSNumber.class] && CFGetTypeID((__bridge CFTypeRef)value) != CFBooleanGetTypeID() &&
        isfinite([value doubleValue]) && [value doubleValue] >= low && [value doubleValue] <= high;
}
static BOOL identifier_ok(id value) {
    if (![value isKindOfClass:NSString.class] || ![value length] || [value length] > 64) return NO;
    NSCharacterSet *allowed = [NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyz0123456789._-"];
    return [value rangeOfCharacterFromSet:allowed.invertedSet].location == NSNotFound &&
        [[NSCharacterSet alphanumericCharacterSet] characterIsMember:[value characterAtIndex:0]];
}
static BOOL rect_ok(id value) {
    if (!fields(value, @[@"x", @"y", @"width", @"height"])) return NO;
    for (NSString *key in @[@"x", @"y"]) if (!number(value[key], 0, 1)) return NO;
    for (NSString *key in @[@"width", @"height"]) if (!number(value[key], 0.01, 1)) return NO;
    return [value[@"x"] doubleValue] + [value[@"width"] doubleValue] <= 1 + geometryTolerance &&
           [value[@"y"] doubleValue] + [value[@"height"] doubleValue] <= 1 + geometryTolerance;
}
static CGRect normalized_rect(NSDictionary *value) {
    return CGRectMake([value[@"x"] doubleValue], [value[@"y"] doubleValue],
                      [value[@"width"] doubleValue], [value[@"height"] doubleValue]);
}

@implementation S2AVPadLayout
+ (instancetype)readData:(NSData *)data error:(NSError **)error {
    if (data.length > 65536) { fail(error, @"F01.size", @"布局文件不能超过 64 KiB"); return nil; }
    id doc = [NSJSONSerialization JSONObjectWithData:data options:0 error:error];
    if (!doc) return nil;
    if (!fields(doc, @[@"schemaVersion", @"id", @"name", @"buttons", @"sticks"]) ||
        !number(doc[@"schemaVersion"], 1, 1) || !identifier_ok(doc[@"id"]) ||
        ![doc[@"name"] isKindOfClass:NSString.class] || ![doc[@"name"] length] || [doc[@"name"] length] > 64 ||
        [doc[@"name"] rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location != NSNotFound ||
        ![doc[@"buttons"] isKindOfClass:NSArray.class] || ![doc[@"sticks"] isKindOfClass:NSArray.class]) {
        fail(error, @"F01.schema", @"布局版本或顶层字段无效（需要 schemaVersion 1）"); return nil;
    }
    NSArray *required = @[@"A", @"B", @"X", @"Y", @"L", @"R", @"ZL", @"ZR", @"PLUS", @"MINUS", @"LEFT", @"UP", @"RIGHT", @"DOWN"];
    NSSet *allowed = [NSSet setWithArray:[required arrayByAddingObjectsFromArray:@[@"LS", @"RS"]]];
    NSMutableSet *seen = [NSMutableSet set];
    NSArray *buttons = doc[@"buttons"];
    if (buttons.count < 14 || buttons.count > 16) { fail(error, @"F01.buttons", @"需要完整的 14 个按键，可额外提供 LS/RS"); return nil; }
    for (id b in buttons) {
        if (!fields(b, @[@"id", @"x", @"y", @"width", @"height", @"cornerRadius"]) ||
            ![b[@"id"] isKindOfClass:NSString.class] || ![allowed containsObject:b[@"id"]] || [seen containsObject:b[@"id"]] ||
            !number(b[@"x"], 0, 1) || !number(b[@"y"], 0, 1) ||
            !number(b[@"width"], 0.04, 0.4) || !number(b[@"height"], 0.04, 0.4) ||
            !number(b[@"cornerRadius"], 0, 0.2) ||
            [b[@"cornerRadius"] doubleValue] > MIN([b[@"width"] doubleValue], [b[@"height"] doubleValue]) / 2 + geometryTolerance) {
            fail(error, @"F01.buttons", @"按键字段、范围或编号无效，不允许重复编号"); return nil;
        }
        [seen addObject:b[@"id"]];
    }
    if (![[NSSet setWithArray:required] isSubsetOfSet:seen]) { fail(error, @"F01.buttons", @"缺少必需按键"); return nil; }
    NSArray *sticks = doc[@"sticks"];
    if (sticks.count != 2) { fail(error, @"F01.sticks", @"需要 left 与 right 两个浮动摇杆"); return nil; }
    [seen removeAllObjects];
    for (id s in sticks) {
        if (!fields(s, @[@"id", @"activation", @"radius"]) ||
            ![s[@"id"] isKindOfClass:NSString.class] || ![@[@"left", @"right"] containsObject:s[@"id"]] ||
            [seen containsObject:s[@"id"]] || !rect_ok(s[@"activation"]) || !number(s[@"radius"], 0.04, 0.3)) {
            fail(error, @"F01.sticks", @"摇杆字段、区域或半径无效"); return nil;
        }
        [seen addObject:s[@"id"]];
    }
    CGRect overlap = CGRectIntersection(normalized_rect(sticks[0][@"activation"]), normalized_rect(sticks[1][@"activation"]));
    if (!CGRectIsNull(overlap) && overlap.size.width > geometryTolerance && overlap.size.height > geometryTolerance) {
        fail(error, @"F01.sticks", @"左右摇杆激活区域不能重叠"); return nil;
    }
    S2AVPadLayout *layout = [self new];
    layout->_document = [doc copy]; layout->_identifier = [doc[@"id"] copy]; layout->_name = [doc[@"name"] copy];
    layout->_buttons = [buttons copy]; layout->_sticks = [sticks copy];
    return layout;
}
+ (BOOL)isBuiltinIdentifier:(NSString *)identifier {
    return [identifier isEqualToString:S2AVPadLayoutDefaultIdentifier] || [identifier isEqualToString:S2AVPadLayoutTouchIdentifier];
}
+ (instancetype)defaultForSafeRect:(CGRect)rect {
    CGFloat w = MAX(rect.size.width, 1), h = MAX(rect.size.height, 1), unit = MIN(w, h);
    // 11-07 维护者 定（参考图 + 方案三「描线」）：肩键下移到约 1/3 高、拇指够得到；左摇杆左侧中部、十字键在它右下；右摇杆右下偏中；ABXY 右侧中下；
    //   L3 / R3 小键挨着两根摇杆；− / + 留顶上正中。位置按安全区比例（横按宽、纵按高），尺寸照旧按点（iPhone 17 Pro Max 横屏 832×419 安全区设计）
    CGFloat ax = 0.905 * w, ay = 0.625 * h, ad = 47, px = 0.235 * w, py = 0.80 * h, pd = 32;
    NSArray *spec = @[@[@"A", @(ax + ad), @(ay)], @[@"B", @(ax), @(ay + ad)], @[@"X", @(ax), @(ay - ad)], @[@"Y", @(ax - ad), @(ay)],
        @[@"ZL", @(0.035 * w), @(0.358 * h)], @[@"L", @(0.135 * w), @(0.358 * h)],
        @[@"R", @(0.865 * w), @(0.358 * h)], @[@"ZR", @(0.965 * w), @(0.358 * h)],
        @[@"MINUS", @(w / 2 - 52), @(22 - rect.origin.y)], @[@"PLUS", @(w / 2 + 52), @(22 - rect.origin.y)],
        @[@"UP", @(px), @(py - pd)], @[@"DOWN", @(px), @(py + pd)], @[@"LEFT", @(px - pd), @(py)], @[@"RIGHT", @(px + pd), @(py)],
        // 摇杆按下（11-06 维护者：七龙珠Z 协议确认页要按）：L3 / R3 挨着各自摇杆的上方；放在其它键之后，热区重叠处仍归旧键（keyAt 按顺序取第一个）
        @[@"LS", @(0.205 * w), @(0.489 * h)], @[@"RS", @(0.795 * w), @(0.489 * h)]];
    NSMutableArray *buttons = [NSMutableArray array];
    for (NSUInteger i = 0; i < spec.count; i++) {
        NSArray *s = spec[i]; CGFloat bw = i < 4 ? 50 : i < 8 ? 58 : i < 10 ? 30 : i < 14 ? 30 : 30;
        CGFloat bh = i < 4 ? 50 : i < 8 ? 32 : i < 10 ? 30 : i < 14 ? 30 : 30, cr = i < 4 ? 25 : i < 8 ? 16 : i < 10 ? 15 : i < 14 ? 8 : 15;
        [buttons addObject:@{@"id": s[0], @"x": @(MAX(0, MIN(1, [s[1] doubleValue] / w))), @"y": @(MAX(0, MIN(1, [s[2] doubleValue] / h))),
            @"width": @(bw / unit), @"height": @(bh / unit), @"cornerRadius": @(cr / unit)}];
    }
    NSDictionary *doc = @{@"schemaVersion": @1, @"id": S2AVPadLayoutDefaultIdentifier, @"name": @"默认布局", @"buttons": buttons,
        @"sticks": @[@{@"id": @"left", @"activation": @{@"x": @0, @"y": @0, @"width": @0.5, @"height": @1}, @"radius": @(60 / unit)},
                     @{@"id": @"right", @"activation": @{@"x": @0.5, @"y": @0, @"width": @0.5, @"height": @1}, @"radius": @(60 / unit)}]};
    // UIKit calls this only for landscape viewports. Do not parse our own trusted defaults on every resize.
    S2AVPadLayout *layout = [self new]; layout->_document = doc; layout->_identifier = S2AVPadLayoutDefaultIdentifier; layout->_name = doc[@"name"];
    layout->_buttons = buttons; layout->_sticks = doc[@"sticks"]; return layout;
}
- (instancetype)layoutWithButtonCenters:(NSDictionary<NSString *, NSArray<NSNumber *> *> *)centers safeRect:(CGRect)rect
                             identifier:(NSString *)identifier name:(NSString *)name error:(NSError **)error {
    CGFloat w = MAX(rect.size.width, 1), h = MAX(rect.size.height, 1);
    NSMutableArray *buttons = [NSMutableArray array];
    for (NSDictionary *button in self.buttons) {
        NSMutableDictionary *b = [button mutableCopy]; NSArray<NSNumber *> *c = centers[button[@"id"]];
        if (c.count == 2) { CGPoint p = CGPointMake(c[0].doubleValue, c[1].doubleValue);
            b[@"x"] = @(MAX(0, MIN(1, (p.x - CGRectGetMinX(rect)) / w))); b[@"y"] = @(MAX(0, MIN(1, (p.y - CGRectGetMinY(rect)) / h))); }
        [buttons addObject:b];
    }
    NSDictionary *doc = @{@"schemaVersion": @1, @"id": identifier, @"name": name, @"buttons": buttons, @"sticks": self.sticks};
    NSData *data = [NSJSONSerialization dataWithJSONObject:doc options:0 error:error];
    return data ? [S2AVPadLayout readData:data error:error] : nil;
}
- (CGRect)frameForButton:(NSDictionary *)button safeRect:(CGRect)rect {
    CGFloat unit = MIN(rect.size.width, rect.size.height);
    CGFloat w = MIN(rect.size.width, [button[@"width"] doubleValue] * unit);
    CGFloat h = MIN(rect.size.height, [button[@"height"] doubleValue] * unit);
    CGFloat x = CGRectGetMinX(rect) + [button[@"x"] doubleValue] * rect.size.width - w / 2;
    CGFloat y = CGRectGetMinY(rect) + [button[@"y"] doubleValue] * rect.size.height - h / 2;
    return CGRectMake(MAX(CGRectGetMinX(rect), MIN(x, CGRectGetMaxX(rect) - w)),
                      MAX(CGRectGetMinY(rect), MIN(y, CGRectGetMaxY(rect) - h)), w, h);
}
- (nullable NSString *)buttonAt:(CGPoint)p safeRect:(CGRect)rect slop:(CGFloat)slop {
    for (NSDictionary *button in _buttons)
        if (CGRectContainsPoint(CGRectInset([self frameForButton:button safeRect:rect], -slop, -slop), p)) return button[@"id"];
    return nil;
}
- (CGRect)activationForStick:(NSDictionary *)stick safeRect:(CGRect)rect {
    CGRect n = normalized_rect(stick[@"activation"]);
    return CGRectMake(rect.origin.x + n.origin.x * rect.size.width, rect.origin.y + n.origin.y * rect.size.height,
                      n.size.width * rect.size.width, n.size.height * rect.size.height);
}
- (CGFloat)radiusForStick:(NSDictionary *)stick safeRect:(CGRect)rect {
    return [stick[@"radius"] doubleValue] * MIN(rect.size.width, rect.size.height);
}
- (NSData *)JSONData:(NSError **)error {
    return [NSJSONSerialization dataWithJSONObject:_document options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:error];
}
@end

BOOL S2AVPadHapticStep(S2AVPadHapticState *state, uint32_t buttons, uint8_t activeSticks, uint8_t edgeSticks, BOOL enabled) {
    activeSticks &= 3; edgeSticks &= activeSticks;
    state->reachedEdges &= activeSticks;
    BOOL pulse = (buttons & ~state->buttons) || (edgeSticks & ~state->reachedEdges);
    state->buttons = buttons; state->reachedEdges |= edgeSticks;
    return enabled && pulse;
}

uint32_t S2AVPadHoldLockStep(S2AVPadHoldLockState *state, uint32_t held, uint32_t lockable, double now, BOOL enabled) {
    if (!enabled) { *state = (S2AVPadHoldLockState){0}; return 0; }
    held &= lockable & 0xFFFF;
    uint32_t before = state->locked, down = held & ~state->held, up = state->held & ~held;
    // 先结算满 2 秒：主线程卡顿时定时器可能排在抬手之后，手指按够了就算数
    for (int i = 0; i < 16; i++) if ((state->held & ~state->locked) >> i & 1 && now - state->downAt[i] >= kS2AVPadHoldLockSeconds) state->locked |= 1u << i;
    for (int i = 0; i < 16; i++) if (down >> i & 1) { if (state->locked >> i & 1) state->unlocking |= 1u << i; else state->downAt[i] = now; }
    state->locked &= ~(up & state->unlocking); state->unlocking &= ~up; state->held = held;
    return before ^ state->locked;
}
double S2AVPadHoldLockDeadline(const S2AVPadHoldLockState *state) {
    double due = 0;
    for (int i = 0; i < 16; i++) if ((state->held & ~state->locked) >> i & 1) {
        double t = state->downAt[i] + kS2AVPadHoldLockSeconds; if (due == 0 || t < due) due = t;
    }
    return due;
}

static BOOL stick_values(CGFloat deadzone, CGFloat saturation, CGFloat exponent) {
    return isfinite(deadzone) && deadzone >= 0 && deadzone <= .4 &&
        isfinite(saturation) && saturation >= .5 && saturation <= 1 &&
        isfinite(exponent) && exponent >= .5 && exponent <= 3;
}
CGPoint S2AVPadStickResponse(CGPoint input, CGFloat deadzone, CGFloat saturation, CGFloat exponent) {
    CGFloat radius = hypot(input.x, input.y);
    if (!isfinite(radius) || radius <= deadzone || !stick_values(deadzone, saturation, exponent)) return CGPointZero;
    CGFloat length = pow(MIN(1, (radius - deadzone) / (saturation - deadzone)), exponent);
    return CGPointMake(input.x / radius * length, input.y / radius * length);
}

// {schemaVersion:1, enabled:布尔} 的小文件。1 = 读到，0 = 没有文件，-1 = 损坏
static int read_enabled(NSURL *file, BOOL *enabled) {
    NSFileManager *fm = NSFileManager.defaultManager;
    if (![fm fileExistsAtPath:file.path]) return 0;
    NSDictionary *attributes = [fm attributesOfItemAtPath:file.path error:nil];
    NSData *data = nil;
    if ([attributes[NSFileType] isEqualToString:NSFileTypeRegular] && [attributes[NSFileSize] unsignedLongLongValue] <= 4096)
        data = [NSData dataWithContentsOfURL:file options:0 error:nil];
    id value = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
    if (!fields(value, @[@"schemaVersion", @"enabled"]) || !number(value[@"schemaVersion"], 1, 1) ||
        ![value[@"enabled"] isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)value[@"enabled"]) != CFBooleanGetTypeID()) return -1;
    *enabled = [value[@"enabled"] boolValue]; return 1;
}

@implementation S2AVPadLayoutStore {
    NSString *_selectionProblem;
    NSString *_hapticProblem;
    NSString *_holdLockProblem;

    NSString *_stickProblem;
    NSString *_visibilityProblem;
}
- (instancetype)initWithDirectory:(NSURL *)directory { return [self initWithDirectory:directory settingsDirectory:directory]; }
- (instancetype)initWithDirectory:(NSURL *)directory settingsDirectory:(NSURL *)settingsDirectory {
    if (!(self = [super init])) return nil;
    _directory = [directory copy]; _settingsDirectory = [settingsDirectory copy]; _selectedIdentifier = S2AVPadLayoutDefaultIdentifier; _diagnostics = @[];
    _hapticFeedbackEnabled = YES;
    if (read_enabled([_settingsDirectory URLByAppendingPathComponent:@".haptics.json"], &_hapticFeedbackEnabled) < 0)
        _hapticProblem = @"F01.haptics：触感反馈设置损坏，暂用默认开启；原记录保留至重新设置成功";
    _holdLockEnabled = YES;
    if (read_enabled([_directory URLByAppendingPathComponent:@".hold_lock.json"], &_holdLockEnabled) < 0)
        _holdLockProblem = @"F01.holdLock：按住锁定设置损坏，暂用默认开启；原记录保留至重新设置成功";
    _idleKeyOpacity = 0.25;
    NSURL *visibility = [_settingsDirectory URLByAppendingPathComponent:@".visibility.json"];
    if ([NSFileManager.defaultManager fileExistsAtPath:visibility.path]) {
        NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:visibility.path error:nil];
        NSData *data = nil;
        if ([attributes[NSFileType] isEqualToString:NSFileTypeRegular] && [attributes[NSFileSize] unsignedLongLongValue] <= 4096)
            data = [NSData dataWithContentsOfURL:visibility];
        id value = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if (fields(value, @[@"schemaVersion", @"idleKeyOpacity"]) && number(value[@"schemaVersion"], 1, 1) && number(value[@"idleKeyOpacity"], 0, 1))
            _idleKeyOpacity = [value[@"idleKeyOpacity"] doubleValue];
        else _visibilityProblem = @"F01.visibility：闲置透明度设置损坏，暂用 25%；重新调整可修复，原记录保留";
    }
    NSURL *url = [_directory URLByAppendingPathComponent:@".selection.json"];
    if ([NSFileManager.defaultManager fileExistsAtPath:url.path]) {
        NSError *error;
        NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:url.path error:&error];
        NSData *data = nil;
        if ([attributes[NSFileType] isEqualToString:NSFileTypeRegular] && [attributes[NSFileSize] unsignedLongLongValue] <= 4096)
            data = [NSData dataWithContentsOfURL:url options:0 error:&error];
        id value = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:&error] : nil;
        if (fields(value, @[@"schemaVersion", @"id"]) && number(value[@"schemaVersion"], 1, 1) && identifier_ok(value[@"id"]))
            _selectedIdentifier = [value[@"id"] copy];
        else _selectionProblem = @"F01.selection：布局选择记录损坏，暂用默认布局；可重新选择修复，原记录保留至选择成功";
    }
    _stickDeadzone = .08; _stickSaturation = _stickExponent = 1;
    NSURL *stickURL = [_settingsDirectory URLByAppendingPathComponent:@".stick.json"];
    if ([NSFileManager.defaultManager fileExistsAtPath:stickURL.path]) {
        NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:stickURL.path error:nil];
        NSData *data = nil;
        if ([attributes[NSFileType] isEqualToString:NSFileTypeRegular] && [attributes[NSFileSize] unsignedLongLongValue] <= 4096)
            data = [NSData dataWithContentsOfURL:stickURL options:0 error:nil];
        id value = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if (fields(value, @[@"schemaVersion", @"deadzone", @"saturation", @"exponent"]) &&
            number(value[@"schemaVersion"], 1, 1) && number(value[@"deadzone"], 0, .4) &&
            number(value[@"saturation"], .5, 1) && number(value[@"exponent"], .5, 3)) {
            _stickDeadzone = [value[@"deadzone"] doubleValue]; _stickSaturation = [value[@"saturation"] doubleValue]; _stickExponent = [value[@"exponent"] doubleValue];
        } else _stickProblem = @"F01.stick：摇杆手感记录无效，暂用默认值；原记录保留至重新设置成功";
    }
    _manualChosen = [NSFileManager.defaultManager fileExistsAtPath:[_directory URLByAppendingPathComponent:@".manual"].path];
    return self;
}
- (NSURL *)urlForIdentifier:(NSString *)identifier {
    return [_directory URLByAppendingPathComponent:[identifier stringByAppendingString:@".json"]];
}
- (S2AVPadLayout *)readIdentifier:(NSString *)identifier error:(NSError **)error {
    NSURL *url = [self urlForIdentifier:identifier];
    NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:url.path error:error];
    if (!attributes) return nil;
    if (![attributes[NSFileType] isEqualToString:NSFileTypeRegular] || [attributes[NSFileSize] unsignedLongLongValue] > 65536) {
        fail(error, @"F01.file", @"布局必须是 64 KiB 以内的普通 JSON 文件"); return nil;
    }
    NSData *data = [NSData dataWithContentsOfURL:url options:0 error:error];
    S2AVPadLayout *layout = data ? [S2AVPadLayout readData:data error:error] : nil;
    if (layout && ![layout.identifier isEqualToString:identifier]) {
        fail(error, @"F01.identity", @"文件名必须与布局 id 一致"); return nil;
    }
    return layout;
}
- (NSArray<S2AVPadLayout *> *)layouts {
    NSMutableArray *result = [NSMutableArray array], *problems = [NSMutableArray array];
    if (_selectionProblem) [problems addObject:_selectionProblem];
    if (_hapticProblem) [problems addObject:_hapticProblem];
    if (_holdLockProblem) [problems addObject:_holdLockProblem];
    if (_stickProblem) [problems addObject:_stickProblem];
    if (_visibilityProblem) [problems addObject:_visibilityProblem];
    NSError *error; NSArray *urls = [NSFileManager.defaultManager contentsOfDirectoryAtURL:_directory includingPropertiesForKeys:nil options:NSDirectoryEnumerationSkipsHiddenFiles error:&error];
    if (!urls && error.code != NSFileReadNoSuchFileError) [problems addObject:error.localizedDescription];
    for (NSURL *url in [urls sortedArrayUsingComparator:^NSComparisonResult(NSURL *a, NSURL *b) { return [a.lastPathComponent compare:b.lastPathComponent]; }]) {
        if (![url.pathExtension.lowercaseString isEqualToString:@"json"]) continue;
        NSString *identifier = url.lastPathComponent.stringByDeletingPathExtension;
        if (!identifier_ok(identifier) || [S2AVPadLayout isBuiltinIdentifier:identifier]) { [problems addObject:@"F01.identity：布局文件名无效或使用保留 id（default / touch）"]; continue; }
        error = nil; S2AVPadLayout *layout = [self readIdentifier:identifier error:&error];
        if (layout) [result addObject:layout]; else [problems addObject:[NSString stringWithFormat:@"%@：%@", url.lastPathComponent, error.localizedDescription]];
    }
    if (![S2AVPadLayout isBuiltinIdentifier:_selectedIdentifier] &&
        ![[result valueForKey:@"identifier"] containsObject:_selectedIdentifier])
        [problems addObject:@"F01.selection：选定布局缺失或无效，当前暂用默认布局；请选择有效方案或恢复默认"];
    _diagnostics = [problems copy]; return result;
}
- (S2AVPadLayout *)selectedLayout:(NSError **)error {
    if (_selectionProblem) { fail(error, @"F01.selection", _selectionProblem); return nil; }
    if ([S2AVPadLayout isBuiltinIdentifier:_selectedIdentifier]) return nil;
    return [self readIdentifier:_selectedIdentifier error:error];
}
- (BOOL)prepareDirectory:(NSError **)error {
    return [NSFileManager.defaultManager createDirectoryAtURL:_directory withIntermediateDirectories:YES attributes:nil error:error];
}
- (BOOL)prepareSettingsDirectory:(NSError **)error {
    return [NSFileManager.defaultManager createDirectoryAtURL:_settingsDirectory withIntermediateDirectories:YES attributes:nil error:error];
}
- (BOOL)replaceLayout:(S2AVPadLayout *)layout error:(NSError **)error {
    if ([S2AVPadLayout isBuiltinIdentifier:layout.identifier]) return fail(error, @"F01.identity", @"default / touch 是内置方案，请另存为新方案");
    NSData *data = [layout JSONData:error];
    if (!data || ![S2AVPadLayout readData:data error:error] || ![self prepareDirectory:error]) return NO;
    return [data writeToURL:[self urlForIdentifier:layout.identifier] options:NSDataWritingAtomic error:error];   // 原子替换：看得见的文件总是完整的
}
- (BOOL)setHapticFeedbackEnabled:(BOOL)enabled error:(NSError **)error {
    if (![self prepareSettingsDirectory:error]) return NO;
    NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"schemaVersion": @1, @"enabled": (enabled ? @YES : @NO)} options:0 error:error];
    if (!data || ![data writeToURL:[_settingsDirectory URLByAppendingPathComponent:@".haptics.json"] options:NSDataWritingAtomic error:error]) return NO;
    _hapticFeedbackEnabled = enabled; _hapticProblem = nil; return YES;
}
- (BOOL)setHoldLockEnabled:(BOOL)enabled error:(NSError **)error {
    if (![self prepareDirectory:error]) return NO;
    NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"schemaVersion": @1, @"enabled": (enabled ? @YES : @NO)} options:0 error:error];
    if (!data || ![data writeToURL:[_directory URLByAppendingPathComponent:@".hold_lock.json"] options:NSDataWritingAtomic error:error]) return NO;
    _holdLockEnabled = enabled; _holdLockProblem = nil; return YES;
}
- (BOOL)setStickDeadzone:(CGFloat)deadzone saturation:(CGFloat)saturation exponent:(CGFloat)exponent error:(NSError **)error {
    if (!stick_values(deadzone, saturation, exponent)) return fail(error, @"F01.stick", @"死区须为 0–0.4，外圈饱和须为 0.5–1，响应指数须为 0.5–3");
    if (![self prepareSettingsDirectory:error]) return NO;
    NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"schemaVersion": @1, @"deadzone": @(deadzone), @"saturation": @(saturation), @"exponent": @(exponent)} options:0 error:error];
    if (!data || ![data writeToURL:[_settingsDirectory URLByAppendingPathComponent:@".stick.json"] options:NSDataWritingAtomic error:error]) return NO;
    _stickDeadzone = deadzone; _stickSaturation = saturation; _stickExponent = exponent; _stickProblem = nil;
    return YES;
}
- (BOOL)setIdleKeyOpacity:(CGFloat)opacity error:(NSError **)error {
    if (!number(@(opacity), 0, 1)) return fail(error, @"F01.visibility", @"闲置透明度必须在 0 到 1 之间");
    if (![self prepareSettingsDirectory:error]) return NO;
    NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"schemaVersion": @1, @"idleKeyOpacity": @(opacity)} options:0 error:error];
    if (!data || ![data writeToURL:[_settingsDirectory URLByAppendingPathComponent:@".visibility.json"] options:NSDataWritingAtomic error:error]) return NO;
    _idleKeyOpacity = opacity; _visibilityProblem = nil; return YES;
}
- (BOOL)saveLayout:(S2AVPadLayout *)layout error:(NSError **)error {
    if ([S2AVPadLayout isBuiltinIdentifier:layout.identifier]) return fail(error, @"F01.identity", @"default / touch 是内置方案，请另存为新方案");
    NSData *data = [layout JSONData:error];
    if (!data || ![S2AVPadLayout readData:data error:error] || ![self prepareDirectory:error]) return NO;
    NSURL *url = [self urlForIdentifier:layout.identifier];
    if ([NSFileManager.defaultManager fileExistsAtPath:url.path]) return fail(error, @"F01.exists", @"同 id 布局已存在，请先更换 JSON 中的 id；原方案未覆盖");
    NSURL *temporary = [_directory URLByAppendingPathComponent:[@".import-" stringByAppendingString:NSUUID.UUID.UUIDString]];
    if (![data writeToURL:temporary options:NSDataWritingAtomic error:error]) return NO;
    // moveItem refuses an existing destination; the visible file is always complete.
    BOOL ok = [NSFileManager.defaultManager moveItemAtURL:temporary toURL:url error:error];
    if (!ok) [NSFileManager.defaultManager removeItemAtURL:temporary error:nil];
    return ok;
}
- (BOOL)selectIdentifier:(NSString *)identifier error:(NSError **)error {
    if (!identifier_ok(identifier)) return fail(error, @"F01.identity", @"布局 id 无效");
    if (![S2AVPadLayout isBuiltinIdentifier:identifier] && ![self readIdentifier:identifier error:error]) return NO;
    if (![self prepareDirectory:error]) return NO;
    NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"schemaVersion": @1, @"id": identifier} options:0 error:error];
    if (![data writeToURL:[_directory URLByAppendingPathComponent:@".selection.json"] options:NSDataWritingAtomic error:error]) return NO;
    _selectedIdentifier = [identifier copy]; _selectionProblem = nil; return YES;
}
- (BOOL)markManual:(NSError **)error {
    if (_manualChosen) return YES;
    if (![self prepareDirectory:error]) return NO;
    if (![[NSData data] writeToURL:[_directory URLByAppendingPathComponent:@".manual"] options:NSDataWritingAtomic error:error]) return NO;
    _manualChosen = YES; return YES;
}
- (NSURL *)lastKeysURL { return [_directory URLByAppendingPathComponent:@".last_keys.json"]; }
- (NSString *)lastKeysIdentifier {   // 记下的按键方案；读不到 / 坏了 / 已失效 → default
    NSData *data = [NSData dataWithContentsOfURL:[self lastKeysURL] options:0 error:nil];
    id value = data.length <= 4096 && data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
    NSString *identifier = fields(value, @[@"schemaVersion", @"id"]) && number(value[@"schemaVersion"], 1, 1) && identifier_ok(value[@"id"]) ? value[@"id"] : nil;
    if (!identifier || [identifier isEqualToString:S2AVPadLayoutTouchIdentifier]) return S2AVPadLayoutDefaultIdentifier;
    if (![S2AVPadLayout isBuiltinIdentifier:identifier] && ![self readIdentifier:identifier error:nil]) return S2AVPadLayoutDefaultIdentifier;
    return identifier;
}
- (int)setTouchOnly:(BOOL)on error:(NSError **)error {
    BOOL now = !_selectionProblem && [_selectedIdentifier isEqualToString:S2AVPadLayoutTouchIdentifier];
    if (now == on) return 0;
    if (on) {
        // 切走前的按键方案（选择记录坏了时 _selectedIdentifier 仍是 default，照记 default）
        if (![self prepareDirectory:error]) return -1;
        NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"schemaVersion": @1, @"id": _selectedIdentifier} options:0 error:error];
        if (!data || ![data writeToURL:[self lastKeysURL] options:NSDataWritingAtomic error:error]) return -1;
        return [self selectIdentifier:S2AVPadLayoutTouchIdentifier error:error] ? 1 : -1;
    }
    return [self selectIdentifier:[self lastKeysIdentifier] error:error] ? 1 : -1;
}
@end
