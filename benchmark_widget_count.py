#!/usr/bin/env python3
"""Check how many widgets are in the compositor's visible map."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

async def main():
    from app import OrchestratorApp

    async with OrchestratorApp().run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await asyncio.sleep(1)
        await pilot.pause()

        screen = pilot.app.screen
        compositor = screen._compositor

        # How many widgets in the visible map?
        visible = compositor.visible_widgets
        print(f"\nVisible widgets in compositor: {len(visible)}")
        for widget, (region, clip) in visible.items():
            name = f"{widget.__class__.__name__}#{widget.id}" if widget.id else widget.__class__.__name__
            print(f"  {name:40s}  region={region}  display={widget.display}")

        # Also check dirty regions
        print(f"\nDirty regions: {len(compositor._dirty_regions)}")


asyncio.run(main())
