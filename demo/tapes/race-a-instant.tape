# Race A, instant take: one prompt, 100 sequential tool calls, the fake model answering at once.
Output demo/out/race-a-instant.mp4
Output demo/out/race-a-instant.webm
Source "demo/tapes/settings.tape"

Type "demo/visual.py a instant"
Sleep 400ms
Enter
Wait+Screen /Race finished/
Sleep 5s
