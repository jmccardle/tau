# Race A, paced take: the fake model takes 400 ms to a first token and streams 70 tokens/s.
Output demo/out/race-a-paced.mp4
Output demo/out/race-a-paced.webm
Source "demo/tapes/settings.tape"

Type "demo/visual.py a paced"
Sleep 400ms
Enter
Wait+Screen /Race finished/
Sleep 5s
