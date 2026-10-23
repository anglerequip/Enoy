# Troubleshooting

Start with the **Diagnostics** screen: it shows the problem and a suggestion for most
of the cases below. The log is at `%LOCALAPPDATA%\FusionAI\logs\fusion.log`.

## Qwen shows "offline" / "Cannot reach Ollama"

Ollama is not running. Start **Ollama** from the Start menu (its icon appears in the
tray), then press **Refresh** on Home. If you changed the port, set the endpoint in
Settings > Local model.

## "Model 'qwen3.5:9b' is not installed"

Enoy never downloads models. Either pick an installed model in Settings, or
download it yourself (about 6.6 GB):

```bash
ollama pull qwen3.5:9b
```

## The first answer takes a minute

The first request after Ollama starts loads the model into memory (about 30–60 s on
an RTX 3070). Later requests start within seconds while the model stays loaded
(Settings > *Keep model loaded for*, default 15 minutes).

## Answers are slow / "Only N% of the model is in GPU memory"

With 8 GB of VRAM, a 9B model plus a large context does not fully fit on the GPU,
and the rest runs on the CPU. In Settings > Local model, lower **Context size**
(e.g. 8192) and close other GPU-heavy programs (games, video editors, browsers with
many tabs). Keep **Thinking mode** off for routine work.

## "Ollama ran out of memory"

Lower the context size, close other programs, or choose a smaller model.

## Claude shows "unavailable"

- *not installed*: Claude Code was not found. Install it, or set its path in Settings.
- *not signed in*: open a terminal and run the command below, then press Refresh.

```bash
claude auth login
```

- *usage limit reached*: your Claude plan's limit was hit. Enoy marks Claude
  unavailable for a minute and keeps working locally; try again after the reset time.
- *turned off*: Settings > Claude > *Use Claude*.

"Signed in, untested" means sign-in was confirmed with `claude auth status`, but no
real request has been made yet. Settings > **Test Claude** makes one tiny request.

## A change cannot be applied: "changed on disk after this change was proposed"

You (or another tool) edited the file after the AI proposed the change. Nothing was
written. Run the task again (*Resume* / follow-up) so the AI sees the current file.

## "The local proposal could not be used"

The model's answer did not follow the edit format, or a SEARCH block did not match the
file exactly. Enoy retries automatically with the error list, then escalates
(if allowed). Smaller, more specific requests help a 9B model. Adding the file to the
task context (Workspace > File > *Add to task context*) also helps.

## A command is "Blocked"

Pipes (`|`), redirection (`>`), chaining (`&&`), variables (`%X%`) and admin/system
tools are never run from Enoy. Run such commands in your own terminal.

## The window does not open

Run `launch\dev.cmd` to see errors in a console. If the native window fails, Enoy
falls back to Edge app mode, then to your default browser. `launch\dev.cmd --browser`
forces the browser.

## "Enoy is already running"

Another copy is open (check the taskbar). If no window is visible, end
`pythonw.exe` in Task Manager, or delete `%LOCALAPPDATA%\FusionAI\instance.json`.

## Tasks show "interrupted"

The app was closed while they ran. Open the task and click **Resume**; it continues
from the saved state (plan, files, failures, check results).
