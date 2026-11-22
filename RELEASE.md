# Release checklist

Target repository **AM3FS/Beacon-Strength-Plotter**

1. Create an empty repository with that exact name under AM3FS. Stage it privately until authorised to publish. Upload the contents of this directory, including the hidden .github directory. Keep credentials, settings and private logs outside the repository.
2. Update APP_VERSION in updates.py and the documentation as appropriate. The installer build reads that version directly.
3. Under **Actions**, run **Windows installer** on the commit to release. The workflow builds the frozen Windows application, compiles its per-user Inno Setup installer and checks that the installer deploys the executable and both PDFs. Download the workflow artifact and extract VK3FS-Beacon-Strength-Plotter-Setup.exe.
4. Smoke-test the installer on Windows 10 or 11. Check launch from Start, operation with the radio connected, coexistence with existing plotter settings and uninstall. The current build configuration produces an unsigned installer. Decide whether to sign it before release.
5. Create a draft GitHub release for the tested commit with a version tag matching the application version, such as v1.0.0. Attach the installer with its original filename and both PDFs. Review the release and repository visibility before publishing.
6. Verify the public download and use an older installed build to test **Setup → Check for updates** once a newer stable release is public. The check uses GitHub's public latest-release API and sends no station settings. Drafts and pre-releases do not trigger updates.

The updater opens the installer download in the default browser. Installation happens when the user runs that file.
