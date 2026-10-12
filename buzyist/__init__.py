r"""
buZyist: Todoist -> BUSY Bar pomodoros, as a Windows tray app.

Click the tray tomato (or press the hotkey, default Ctrl+Alt+P, which cycles
window -> mini timer -> tray), pick a task from Todoist, and the BUSY Bar
runs its standard interval timer. While it runs:

  * the window and an always-on-top mini timer show the task, the
    phase and the countdown; the tray icon shows the minutes left;
  * the task gets a Todoist label (default "pomodoro");
  * when the session ends, the finished pomodoros are logged as a comment and
    you can complete the task in one click.

Settings, the running session and history live in %APPDATA%\Busyist (the name from
before buZyist, kept so the loaded browser extension stays put) and can
all be changed from the window. busyist.py starts it (main.py); CLAUDE.md
maps the modules. Importing the package imports nothing else.
"""
