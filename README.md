# BuyBot

Windows GUI automation helper for repetitive in-game buy loops. The bot keeps clicking on user-defined ROIs, reads prices via OCR, and logs trades while respecting price and balance guardrails.

## Requirements
- Python 3.11+
- Tesseract OCR installed and available on `PATH`
- Display scaling set so the captured ROIs match what the game shows

Install Python dependencies:

```bash
pip install -r requirements.txt
```

If Tesseract is not already installed, download the Windows installer from [UB Mannheim builds](https://github.com/UB-Mannheim/tesseract/wiki) and ensure `tesseract.exe` is on `PATH`.

## Running
```bash
python main.py
```

### Workflow
1. Open the **Configure** tab, click each ROI selector (Simple: Item, Price, **Total Price**, Max, Buy, Close, Balance. Bulk: Confirm, Cancel, Buy, Balance, Price) and drag the matching on-screen rectangle. Right-click or press `Esc` to cancel.
2. Return to the **Main** tab. Enter `Max Price`, `Current Balance`, `Balance Floor`, and choose a **Buy Method** (Simple works today; Bulk mode will arrive later).
3. Pick the **Target Window** from the dropdown (use *Refresh* if the game is not listed, or type the window title manually). The bot waits until this window is focused and pauses whenever focus is lost.
4. Hit **Start**. Keep the target window focused while the bot runs. Use **Stop** or move the mouse to the top-left corner (PyAutoGUI failsafe) to abort immediately.
5. Toggle **Show Debug Log** under the log table to watch every state change, click, and price read in real time.
6. Turn **Randomize clicks inside ROI** on only if you want each click to land at a different point; it is off (center clicks) by default.
7. Use **Skip BUY click (for testing)** when you want to dry-run the loop without pressing BUY; the log still records the read prices so you can validate OCR.
8. The log table shows timestamp, unit price, total price, and balance so you can verify each deduction.
9. Fine-tune waits in the **Delays** tab (Item open, Close-to-item, Overlay dismiss click, Post-overlay wait). The overlay dismiss delay now defaults to 1 ms so BUY clicks can spam quickly; raise it only if the UI needs breathing room. Changes save automatically; use *Reset to defaults* anytime.
10. Use the **Debug** tab for a structured timeline (timestamp, state, message) when you need to investigate slow BUY spam or other timing issues. The Balance ROI is polled continuously to keep the `Current Balance` field synchronized (values like `17,929K` are treated as `17,929,000`).

The bot state machine follows the spec:
- `IDLE -> CLICK_ITEM -> CHECK_PRICE`
- Out-of-range prices trigger `Close` and a wait, then the loop restarts.
- In-range prices execute `MAX -> BUY`, then re-check price and balance, chaining additional buys while conditions hold.
- Any time you press **Stop**, the bot returns to `IDLE` cleanly.

### Files
- `settings.json` sits next to the executable and keeps ROIs (including Total Price), price/balance guards, and the target window name.
- `trades.csv` appends `timestamp,price,spent,balance_after` for every successful buy.

### Failure Handling
- OCR makes up to 3 attempts and averages readable values; if all attempts fail you get a blocking error dialog and the automation stops.
- Failed clicks, focus loss, or window issues raise a status message and automatically retry once the window is back in focus.

## Notes
- Keep the game resolution/static layout stable so ROIs do not drift.
- The UI stays responsive because automation work happens on a dedicated Qt thread.
- Balance updates mirror the Total Price spent so you can stop when `Balance Floor` is reached.

