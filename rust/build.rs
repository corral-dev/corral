fn main() {
    // Homebrew expands the dylib ID to its installed path after unpacking the wheel.
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        println!("cargo:rustc-cdylib-link-arg=-Wl,-headerpad_max_install_names");
    }
}
