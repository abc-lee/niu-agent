// vibrancy.mm — T0-macOS 原型 N-API 原生模块（NSVisualEffectView + maskImage）
// 所有导出函数均为同步调用（N-API 回调运行在 main thread，AppKit 调用安全）。
#include <node_api.h>
#include <string.h>
#include <stdio.h>
#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>

static const char *kAssocKey = "kT0VibrancyKey";
static void *kAssocPtr = (void *)kAssocKey;

static void *handleOf(napi_env env, napi_value v) {
  void *data = nullptr;
  size_t len = 0;
  if (napi_get_buffer_info(env, v, &data, &len) != napi_ok) return nullptr;
  if (len < 8) return nullptr;
  int64_t p;
  memcpy(&p, data, 8);
  return (void *)p;
}

static void resolve(napi_env env, napi_value v, NSWindow **win, NSView **cv) {
  void *p = handleOf(env, v);
  if (!p) return;
  id obj = (id)p;
  if ([obj isKindOfClass:[NSView class]]) {
    *cv = (NSView *)obj;
    *win = [(NSView *)obj window];
  } else if ([obj isKindOfClass:[NSWindow class]]) {
    *win = (NSWindow *)obj;
    *cv = [win contentView];
  }
}

static NSVisualEffectView *vbOf(NSWindow *w) {
  if (!w) return nil;
  return (NSVisualEffectView *)objc_getAssociatedObject(w, kAssocPtr);
}

static napi_value strRet(napi_env env, const char *s) {
  napi_value v;
  napi_create_string_utf8(env, s, NAPI_AUTO_LENGTH, &v);
  return v;
}
static napi_value errRet(napi_env env, const char *msg) {
  napi_throw_error(env, nullptr, msg);
  return nullptr;
}

// 1) debugHandle(buf) -> 类名字符串（含 super 类）
static napi_value napi_debugHandle(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value a[1];
  napi_get_cb_info(env, info, &argc, a, nullptr, nullptr);
  void *p = handleOf(env, a[0]);
  if (!p) return errRet(env, "bad handle buffer");
  id obj = (id)p;
  Class c = object_getClass(obj);
  NSMutableString *out = [NSMutableString stringWithUTF8String:class_getName(c)];
  Class sc = class_getSuperclass(c);
  if (sc) {
    [out appendString:@" : "];
    [out appendString:[NSString stringWithUTF8String:class_getName(sc)]];
  }
  return strRet(env, [out UTF8String]);
}

// 2) installVibrancy(buf, material, stateActive)
static napi_value napi_installVibrancy(napi_env env, napi_callback_info info) {
  size_t argc = 3;
  napi_value a[3];
  napi_get_cb_info(env, info, &argc, a, nullptr, nullptr);
  napi_valuetype t1;
  napi_typeof(env, a[1], &t1);
  char mat[64] = "under-window";
  if (t1 == napi_string) {
    size_t n = 0;
    napi_get_value_string_utf8(env, a[1], mat, sizeof mat, &n);
  }
  bool stateActive = true;
  napi_get_value_bool(env, a[2], &stateActive);

  NSWindow *win = nullptr;
  NSView *cv = nullptr;
  resolve(env, a[0], &win, &cv);
  if (!win || !cv) return errRet(env, "handle resolve failed");

  NSVisualEffectView *old = vbOf(win);
  if (old) [old removeFromSuperview];

  NSRect b = [cv bounds];
  NSVisualEffectView *v = [[NSVisualEffectView alloc] initWithFrame:b];
  v.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
  v.blendingMode = NSVisualEffectBlendingModeBehindWindow;
  if (strncmp(mat, "hud", 3) == 0) {
    v.material = NSVisualEffectMaterialHUDWindow;
  } else {
    v.material = NSVisualEffectMaterialUnderWindowBackground;
  }
  if (stateActive) {
    v.state = NSVisualEffectStateActive;
  } else {
    v.state = NSVisualEffectStateFollowsWindowActiveState;
  }
  v.alphaValue = 0.0;
  v.maskImage = nil;
  [cv addSubview:v positioned:NSWindowBelow relativeTo:nil];
  objc_setAssociatedObject(win, kAssocPtr, v, OBJC_ASSOCIATION_RETAIN_NONATOMIC);

  char buf[192];
  snprintf(buf, sizeof buf, "ok cv=%s win=%s frame=%.0fx%.0f",
           class_getName([cv class]), class_getName([win class]),
           b.size.width, b.size.height);
  return strRet(env, buf);
}

// 3) setMask(buf, rectsJSON, viewW, viewH) — rects 为 CSS 顶左原点
static napi_value napi_setMask(napi_env env, napi_callback_info info) {
  size_t argc = 4;
  napi_value a[4];
  napi_get_cb_info(env, info, &argc, a, nullptr, nullptr);
  char json[8192];
  size_t nj = 0;
  napi_get_value_string_utf8(env, a[1], json, sizeof json, &nj);
  double vw = 0, vh = 0;
  napi_get_value_double(env, a[2], &vw);
  napi_get_value_double(env, a[3], &vh);

  NSWindow *win = nullptr;
  NSView *cv = nullptr;
  resolve(env, a[0], &win, &cv);
  NSVisualEffectView *v = vbOf(win);
  if (!v) return errRet(env, "no vibrancy view installed");

  NSImage *img = [[NSImage alloc] initWithSize:NSMakeSize(vw, vh)];
  [img lockFocus];
  [[NSColor clearColor] setFill];
  [[NSBezierPath bezierPathWithRect:NSMakeRect(0, 0, vw, vh)] fill];
  int n = 0;
  NSData *d = [NSData dataWithBytes:json length:nj];
  NSArray *arr = [NSJSONSerialization JSONObjectWithData:d options:0 error:nil];
  if (arr) {
    for (NSDictionary *o in arr) {
      double x = [o[@"x"] doubleValue];
      double y = [o[@"y"] doubleValue];
      double w = [o[@"w"] doubleValue];
      double h = [o[@"h"] doubleValue];
      double r = [o[@"r"] doubleValue];
      if (w <= 0 || h <= 0) continue;
      NSRect rc = NSMakeRect(x, vh - y - h, w, h);
      NSBezierPath *p =
          [NSBezierPath bezierPathWithRoundedRect:rc xRadius:r yRadius:r];
      [[NSColor whiteColor] setFill];
      [p fill];
      n++;
    }
  }
  [img unlockFocus];
  v.maskImage = img;
  char out[64];
  snprintf(out, sizeof out, "ok rects=%d", n);
  return strRet(env, out);
}

// 4a) setAlpha(buf, alpha)
static napi_value napi_setAlpha(napi_env env, napi_callback_info info) {
  size_t argc = 2;
  napi_value a[2];
  napi_get_cb_info(env, info, &argc, a, nullptr, nullptr);
  double alpha = 0;
  napi_get_value_double(env, a[1], &alpha);
  NSWindow *win = nullptr;
  NSView *cv = nullptr;
  resolve(env, a[0], &win, &cv);
  NSVisualEffectView *v = vbOf(win);
  if (!v) return errRet(env, "no vibrancy view installed");
  v.alphaValue = alpha;
  return strRet(env, "ok");
}

// 4b) fadeTo(buf, alpha, ms)
static napi_value napi_fadeTo(napi_env env, napi_callback_info info) {
  size_t argc = 3;
  napi_value a[3];
  napi_get_cb_info(env, info, &argc, a, nullptr, nullptr);
  double alpha = 0, ms = 0;
  napi_get_value_double(env, a[1], &alpha);
  napi_get_value_double(env, a[2], &ms);
  NSWindow *win = nullptr;
  NSView *cv = nullptr;
  resolve(env, a[0], &win, &cv);
  NSVisualEffectView *v = vbOf(win);
  if (!v) return errRet(env, "no vibrancy view installed");
  __block NSVisualEffectView *bv = v;
  [NSAnimationContext runAnimationGroup:^(NSAnimationContext *ctx) {
    ctx.duration = ms / 1000.0;
    ctx.timingFunction =
        [CAMediaTimingFunction functionWithName:kCAMediaTimingFunctionEaseInEaseOut];
    bv.animator.alphaValue = alpha;
  }];
  return strRet(env, "ok");
}

// 5) removeVibrancy(buf)
static napi_value napi_removeVibrancy(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value a[1];
  napi_get_cb_info(env, info, &argc, a, nullptr, nullptr);
  NSWindow *win = nullptr;
  NSView *cv = nullptr;
  resolve(env, a[0], &win, &cv);
  NSVisualEffectView *v = vbOf(win);
  if (!v) return strRet(env, "not-installed");
  [v removeFromSuperview];
  objc_setAssociatedObject(win, kAssocPtr, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
  return strRet(env, "ok");
}

// 6) isAvailable() -> true
static napi_value napi_isAvailable(napi_env env, napi_callback_info info) {
  napi_value b;
  napi_get_boolean(env, true, &b);
  return b;
}

static napi_value Init(napi_env env, napi_value exports) {
  napi_value f;
  napi_create_function(env, "debugHandle", NAPI_AUTO_LENGTH, napi_debugHandle, nullptr, &f);
  napi_set_named_property(env, exports, "debugHandle", f);
  napi_create_function(env, "installVibrancy", NAPI_AUTO_LENGTH, napi_installVibrancy, nullptr, &f);
  napi_set_named_property(env, exports, "installVibrancy", f);
  napi_create_function(env, "setMask", NAPI_AUTO_LENGTH, napi_setMask, nullptr, &f);
  napi_set_named_property(env, exports, "setMask", f);
  napi_create_function(env, "setAlpha", NAPI_AUTO_LENGTH, napi_setAlpha, nullptr, &f);
  napi_set_named_property(env, exports, "setAlpha", f);
  napi_create_function(env, "fadeTo", NAPI_AUTO_LENGTH, napi_fadeTo, nullptr, &f);
  napi_set_named_property(env, exports, "fadeTo", f);
  napi_create_function(env, "removeVibrancy", NAPI_AUTO_LENGTH, napi_removeVibrancy, nullptr, &f);
  napi_set_named_property(env, exports, "removeVibrancy", f);
  napi_create_function(env, "isAvailable", NAPI_AUTO_LENGTH, napi_isAvailable, nullptr, &f);
  napi_set_named_property(env, exports, "isAvailable", f);
  return exports;
}
NAPI_MODULE(vibrancy, Init)
