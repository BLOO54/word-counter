# Word Counter

A small Windows app that counts every time a word you pick is said out loud.

![Word Counter window](https://github.com/BLOO54/word-counter/releases/latest/download/window.png)

## Install
1. Go to **Releases** (right side of this page) and download `WordCounter-Setup-x.x.x.exe`.
2. Double-click it. If Windows says *"Windows protected your PC"*, click **More info → Run anyway**.
3. No administrator rights needed.

## How to use
1. Type a word (or a phrase like "no way") and press **Add**. Add as many as you like.
2. Choose what to listen to:
   - **My microphone** – you (and anyone near you)
   - **PC sound** – everything your PC plays: Discord calls, games, YouTube, streams
   - **Both**
3. Press **Start Listening**. Each time a word is heard its counter goes up (and it beeps).

Other bits: **+1 / −1 / Reset** on each word, **Reset all counts**, **Always on top** to keep it over a game,
and your words and counts are saved automatically.

## Good to know
- Speech recognition runs **on your own PC** – nothing is uploaded and no internet is needed.
- It counts whole words only: "bro" won't count inside "brother".
- Counting happens when the speaker pauses, so a counter can go up a second or two after the word.
- Unusual slang can be misheard. Watch the "What I'm hearing" box to see how it spells things,
  and add that spelling as a word if needed.
- Settings and a log file are in `%APPDATA%\WordCounter`.

## Building
Every push to `main` is built on GitHub's Windows machines (see `.github/workflows/build-windows.yml`),
tested (including a hearing test with a computer voice), and published as a release.
