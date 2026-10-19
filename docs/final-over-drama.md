# Final-over chases

The approach flow stays bowler → bowling approach → batting approach. In the
last over of a live chase, the bot then simulates, saves and reveals one delivery
at a time. Each new ball message replaces the previous one and shows the score,
runs needed, balls remaining and any free hit. The completed over summary stays
available. This also works for The Hundred's last five-ball set.

`FINAL_OVER_BALL_DELAY_SECONDS` controls the suspense pause (default 2.5 seconds,
clamped to 0–10). Wides and no-balls get their own reveal without consuming a
legal ball. A wicket still pauses for a human's incoming-batter choice. Saved
partial overs resume after the last recorded delivery, including its free hit.
Message failures do not reroll a delivery or prevent the innings from finishing.

For big final-over asks, additional bowling-error pressure ramps up above a
required rate of 12 per six balls and caps at 24. It raises the extras weight
and transfers some bye/leg-bye probability to wides and no-balls. The chase
controller prices the actual free-delivery share when calculating its outlook.
The bonus is recalculated after every delivery.

A batter rated below 60 gains an effective batting bonus of up to eight points
after 25 balls faced, increasing to twelve at 40, capped at an effective 60
before the usual settling bonuses. This is separate from short-term
rhythm and does not alter their card rating. Both the live engine and the AI
captain's estimate use it. The chase model also gives 25-ball and 40-ball batters
stronger settling credit than a batter who has faced only ten.

The close-finish rule interprets a very small losing margin as 1–3 runs: with
at most four needed from the final legal ball and either active batter having
faced at least 40 balls, only scores that tie or win are eligible. Wides/no-balls
remain possible and preserve the legal delivery. Larger asks retain a chance
of defeat; dismissed batters do not qualify. Live win/tie estimates apply the
same rule.

Focused regression coverage is in `tests/test_final_over_drama.py`.
