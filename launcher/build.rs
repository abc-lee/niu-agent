fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../ui/main/windows/assistant/icons/icon.ico");
        res.set("FileDescription", "Niu Launcher");
        res.set("ProductName", "Niu");
        res.set("LegalCopyright", "MIT License");
        if let Err(e) = res.compile() {
            eprintln!("cargo:warning=winresource failed to compile: {e}");
        }
    }
}
