{
  "targets": [
    {
      "target_name": "vibrancy",
      "sources": ["vibrancy.mm"],
      "defines": ["NAPI_VERSION=8"],
      "xcode_settings": {
        "CLANG_CXX_LANGUAGE_STANDARD": "c++17",
        "CLANG_CXX_LIBRARY": "libc++",
        "GCC_ENABLE_OBJC_ARC": "YES",
        "FRAMEWORK_SEARCH_PATHS": ["/System/Library/Frameworks"],
        "OTHER_LDFLAGS": [
          "-framework", "AppKit",
          "-framework", "Foundation",
          "-framework", "QuartzCore"
        ]
      }
    }
  ]
}
