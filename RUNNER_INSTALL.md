# Jagex Name Runner userscript

This companion runs in Tampermonkey on the official Jagex account site and talks only to the local runner API at `http://127.0.0.1:8765`. It does not send page cookies, credentials, or session tokens to the local API. It fills the display-name field to trigger Jagex's availability check, but never clicks a submit, save, confirm, or claim button.

## Install

1. Install Tampermonkey in Brave.
2. Create a new userscript and paste in `jagex_name_runner.user.js` (or open the file in the editor and copy it).
3. Save it, then open the official Jagex account character-name change page while signed in. The runner panel appears at the lower right.
4. Start the local API service before pressing **Start**. Use **Pause**, **Resume**, and **Stop** to control the queue. A positive result pauses automatically so the name can be reviewed and claimed manually.

The script matches the official `https://secure.runescape.com/m=displaynames/name` path, the Jagex account host, and the legacy secure RuneScape login host. If the signed-in interface redirects to another official host, add a narrow `@match` for that exact host in the userscript header.

## Site adapter calibration

`findNameField()` recognizes the editable name input. The adapter watches the form around that input, the visible “Character name is not available” message, the **Set Name** button, and the brief checkmark. Jagex does not echo the searched name in its message, so the script requires a new validation transition after setting the exact candidate. A completed same-origin request can confirm a repeated error message whose text does not change.

If the page changes, inspect its form and update the adapter functions near `findNameField()`. An unclear result leaves the candidate pending and pauses the runner.

## Result rules

- **Taken:** the visible red name-unavailable message appears or updates after the candidate was entered, or remains after that candidate's fresh same-origin check completes.
- **Available:** the red message is absent and the **Set Name** button or checkmark moves into its usable state after the candidate was entered.
- **Unclear:** the field changes unexpectedly, the form disappears, or no fresh transition arrives within 45 seconds. The lease stays pending.

Each candidate is entered with one input/change update; the script does not clear the field first. A direct one-off defers an interrupted pending lease and restores it to the queue afterward. The script reads visible Jagex time and attempt counters when present and stops on expiry or throttling.

## Backend contract notes

The userscript uses the local endpoints and payloads described by the runner contract. `/api/runner/oneoff` must return the same leased shape as `/api/runner/next` (`candidate.id`, `candidate.name`, and `lease_id`) for a one-off verdict to be reportable. It will refuse to invent a candidate ID when that response is incomplete.
