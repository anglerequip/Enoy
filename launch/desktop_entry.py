"""Shared frozen entry point. The tools executable retains real stdio."""
import runpy
import sys

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--module":
        module = sys.argv[2]
        if module not in ("enoy.tools.mcp_server", "enoy.blender_bridge"):
            raise SystemExit("Unknown Enoy helper module.")
        sys.argv = [module]
        runpy.run_module(module, run_name="__main__")
    else:
        from enoy.app import main
        raise SystemExit(main())
