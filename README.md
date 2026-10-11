# File Organizer

File Organizer is a Windows desktop app that sorts files into type-based
subfolders. The GUI is the primary entry point; the legacy command-line script
is retained as a reference.

## Requirements

- Windows
- Node.js
- pnpm
- Rust and Cargo
- Python 3.11 or newer
- PyInstaller

## Development

Install the JavaScript dependencies and start the Tauri development app:

```powershell
pnpm install
pnpm tauri dev
```

## Build

Build the Python sidecar, then create the Tauri application bundle:

```powershell
python backend/build.py
pnpm tauri build
```

## Using the GUI

1. Choose a source folder with the folder picker.
2. Enable **Include subfolders** to organize files recursively while
   preserving the folder structure. Leave it off to process only files in the
   selected folder.
3. Set any run options, then select **Start**.
4. Review the results and any queued items that need a decision.
5. Select **Undo** to reverse the most recent organize run.

### Run options

- **Verify with SHA-256** compares file hashes after copying. It provides
  stronger verification and takes longer than the default size check.
- **Dry run** previews the planned changes without moving files.
- **Verbose log** shows per-file operations while organizing.

### Undo

Undo applies to the most recent organize run only. The journal is stored at
`%APPDATA%/file-organizer/undo.jsonl`. If a journal from an earlier app session
is found at startup, the app asks whether to undo those changes. Accepting
restores the previous file layout; dismissing the prompt leaves the recovery
available through **Review recovered undo**.

## Tests

Run the backend filesystem tests with:

```powershell
pytest backend/tests -q
```

Tests create their sample folders in temporary directories and isolate the
undo journal from the user's app data.

## Legacy CLI reference

`file_organizer.py` is kept as a reference implementation and is not the
primary way to use the app. Its command syntax is:

```powershell
python file_organizer.py <folder_path> [more_folder_paths...] [--verify-hash] [--dry-run] [--verbose]
```
