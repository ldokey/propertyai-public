# Google Drive photo archive

This integration archives original cleaning and issue photos in Google Drive.
The canonical target is resolved from the Notion integration registry by the
stable resource code `DRIVE.OPS.CLEANING`, not by a mutable folder title.

OAuth is intentionally isolated from Gmail/Calendar:

- Gmail/Calendar token: `secrets/google/token.json`
- Drive token: `secrets/google/drive-token.json`
- Drive scope: `drive.file`

The Drive token can only work with files created by or explicitly made
available to PropertyAI Local. If the pre-existing canonical folder is not
visible, PropertyAI creates its own managed folder and the operator moves that
folder under `01_청소 완료 사진` once. It remains app-accessible afterward.

The current Notion registry target is:

- Resource code: `DRIVE.OPS.CLEANING`
- Folder: `01_청소 완료 사진`
- Folder ID: `1UHPwJWLMAK5wg3eriHUVoizpXYlU7wKR`

Upload completion requires all of the following before local cleanup:

1. Drive returns a file ID.
2. The returned parent is the configured managed folder.
3. Remote size and checksum match the local file.
4. The Drive ID and URL have been recorded in the relevant Notion row.
5. The local original waits in a 24-hour quarantine before deletion.

## Production evidence flow

For a production Telegram issue report, PropertyAI creates one idempotent
Cleaning folder and an issue subfolder beneath the managed root:

```text
00_PropertyAI_자동업로드/
  YYYY-MM-DD_숙소닉네임_퇴실청소/
    01_청소완료/
    02_문제·하자/
```

The Cleaning page stores the Drive file IDs, Cleaning folder ID and URL, and
the issue-folder link. The existing `문제 사진` property receives stable Drive
links; the files are not uploaded into Notion a second time. TEST sessions make
no Drive or Notion writes. Verified production originals move to a private
24-hour local quarantine, and the Telegram service removes only expired files
whose Drive upload and Notion recording flags are both present.

Completion reporting has an additional payment guard:

1. The cleaner presses the completion button.
2. At least four completion photos are submitted.
3. Drive upload and readback verification succeed.
4. The Cleaning row becomes `완료 보고` with `관리자 확인=false`.
5. The operator reviews the linked Drive folder.
6. Only operator approval changes the Cleaning to `관리자 확인 완료` and
   creates the separate transfer-confirmation action.
7. Payment still changes only after the operator confirms that the actual bank
   transfer has already occurred.
