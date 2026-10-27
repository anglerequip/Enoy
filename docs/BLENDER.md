# Blender with ChatGPT and Claude

Enoy can give ChatGPT / Codex and Claude the same local MCP for Blender tools.
Enable **Settings → Models → Shared Blender workspace** and use **Check Blender
connection**. Blender must be open with its **MCP for Blender** server running.
The default connection is `127.0.0.1:9876`.

The installed MCP for Blender executable is detected automatically, or can be
selected explicitly in Settings. Enoy starts a small local bridge for each
provider. The bridge forwards the upstream tool schemas, text, images, resource
links and MCP app metadata. It does not require an MCP SDK in Enoy's environment.

Use Auto, ChatGPT, or Claude in a project chat or a general chat. For example:

> Inspect the current Blender scene. Build a low-poly fishing reel, preserving
> existing objects. Check the viewport and save the .blend file in this project.

Then switch provider in the same Enoy chat:

> Inspect what the previous provider built, refine the handle, and check the
> viewport before saving.

Both providers receive the shared conversation, task results and recent Blender
tool activity. Each should inspect the live scene after a handoff. Separate
Enoy provider turns are serialized, as are individual Blender tool calls across
bridge processes. Parallel chats wait for the active Enoy Blender turn to finish.
External clients using the same bridge share the tool lock and journal; their
entire multi-step conversations are not locked. Hand those conversations off
sequentially. Clients that bypass the bridge do not participate in its locking.

Scene edits happen immediately in Blender and are not Enoy patch proposals or
Enoy undo backups. Specify the destination for saved models and exports; a
selected project supplies the normal output folder. The connection itself does
not create, overwrite, export, or save models.

The connection follows Enoy's cloud policy, private document/file filters, and
tool enable setting. It can be used without granting native shell or file tools
in a general chat. Both providers retain their CLI permission checks. Turning
the Enoy Blender switch off also disables the inherited `blender` MCP entry for
Enoy's Codex requests. The separate Codex desktop connection remains available.

The local handoff journal keeps 40 bounded, redacted activity records under
`%LOCALAPPDATA%\Enoy\blender\9876\activity.json` (or `ENOY_HOME`). The latest
eight records accompany the next Blender-enabled provider request. Images are
forwarded to the caller, not copied into the journal. Native provider activity
also appears in Enoy's existing Activity view.

If the connection check fails, open Blender, press **N** in the 3D viewport,
select **MCP for Blender**, and click **Start MCP Server**. Match the add-on port
to Enoy's configured port. Enoy reports the actual failure rather than treating
the scene as changed. Restart Enoy after updating its Python source; refresh
Settings after changing connection details. Restart the Codex MCP server or
Codex app after changing its MCP configuration.
