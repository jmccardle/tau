# Race B, instant take: reopen a 200-turn session in the TUI until its last message is drawn.
Output demo/out/race-b-instant.mp4
Output demo/out/race-b-instant.webm
Source "demo/tapes/settings.tape"

Type "demo/visual.py b instant"
Sleep 400ms
Enter
Wait+Screen /Race finished/
Sleep 5s
