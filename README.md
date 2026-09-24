# India Fares: on-demand fare checker for Android

A phone app (installable website) with one button. Tap **Check prices now** and it searches
Google Flights across all airlines for YYZ to MAA / BLR / BOM / COK / TRV / CCJ, applies your
rules, and shows the top 3 family fares. Options that beat your previous lowest fare, or got
cheaper since they were last seen, are highlighted in teal. Nothing runs unless you tap.

## Two tabs
- **Check now**: the Check button and the top 3 fares from your latest check.
- **All-time lows**: the 20 cheapest itineraries any of your checks has ever found, each shown
  at its lowest price with the date it was found, how many checks it appeared in, and whether it
  is still at that price or has gone up since. Up to 200 are remembered in `state.json`;
  to start the list fresh, delete `state.json` from the repo.
  Long-press the app icon for an **All-time lows** shortcut.

## Rules applied (each direction)
Direct; 1 stop with a 6 h+ layover; 2 stops with 4 h+ each; 35 h max. Self-transfers and
airport changes are skipped. 2 adults + children aged 7 and 2 (both child fares with seats).
Dates: leave Nov 23 to Dec 7, return Jan 1 to 14. Change any of this in `config.yaml`.

## One-time setup (about 20 minutes, from a computer)

1. **SerpApi key**: sign up at serpapi.com (free plan is 250 searches/month) and copy the API key.
2. **GitHub repo**: create a **public** repo named `fare-watch` and upload all these files,
   including the `.github` folder.
3. **Secret**: repo Settings > Secrets and variables > Actions > New secret:
   `SERPAPI_KEY` = your key.
4. **Website**: Settings > Pages > Deploy from a branch > `main` / `docs` > Save.
   After a minute your app is at `https://<your-username>.github.io/fare-watch/`.
5. **Token for the Check button**: GitHub > Settings (your profile) > Developer settings >
   Personal access tokens > Fine-grained tokens > Generate.
   - Repository access: Only select repositories > `fare-watch`
   - Permissions: **Actions: Read and write**, **Contents: Read-only**
   - Expiration: 90 days is fine (the app tells you if it expires)

## On your Android phone

1. Open the app URL in **Chrome**, tap ⋮ > **Add to Home screen** > Install.
2. Open it, tap the ⚙ gear, enter your GitHub username, repo name and the token, tap **Save and test**.
3. Tap **Check prices now**. It takes 2 to 4 minutes; you can switch apps and come back.

### One-tap check from the home screen
Long-press the **Fares** icon > drag **Check prices now** onto the home screen.
Tapping that icon opens the app and starts a check straight away.

### Real widget (optional)
Websites can't create Android widgets. For a true widget button, install the free
**HTTP Shortcuts** app (Play Store, open source) and create a shortcut:
- Method `POST`, URL `https://api.github.com/repos/<username>/fare-watch/actions/workflows/track.yml/dispatches`
- Headers: `Authorization: Bearer <token>` and `Accept: application/vnd.github+json`
- Body (JSON): `{"ref":"main","inputs":{"mode":"sweep"}}`
- Response: show a toast "Check started"
Then add an HTTP Shortcuts widget to your home screen. It starts a check in the background;
open the Fares app about 3 minutes later to see the results.

## Cost
Each check uses about 56 searches (25 date pairs, plus re-pricing the top 3 to get the real
adult/child split). Free plan: about 4 checks a month. Starter plan (US$25, 1,000 searches):
about 17 checks. GitHub, the website and the app are free.

To spend less per check, set `date_step_days: 4` and `breakdown_top_n: 0` in `config.yaml`
(about 20 searches, estimated per-ticket split). To search more dates, set it to `2` (about 118).

## Notes
- Each check covers every 3rd date across your windows and shifts one day next time,
  so three checks cover every departure date.
- "New low" means cheaper than the lowest fare any earlier check found.
  "Since last check" compares the same flights on the same dates.
- Prices are live at check time; confirm on Google Flights before paying.
- Test without spending searches: `FARE_WATCH_MOCK=sample.json python tracker.py`
