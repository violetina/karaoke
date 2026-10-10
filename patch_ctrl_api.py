import sys

with open("src/karaoke/ctrl_api.py", "r") as f:
    content = f.read()

html_endpoints = """@app.get("/stage", response_class=HTMLResponse)
@app.get("/tv", response_class=HTMLResponse)
def stage_page() -> HTMLResponse:
    \"\"\"Dedicated full-screen stage view for TV/prompter displays.\"\"\"
    from . import stage_view
    return HTMLResponse(stage_view.render_stage_html())

@app.get("/coverart", response_class=HTMLResponse)
def coverart_page() -> HTMLResponse:
    from . import stage_view
    return HTMLResponse(stage_view.render_coverart_html())

@app.get("/dancers", response_class=HTMLResponse)
def dancers_page() -> HTMLResponse:
    from . import stage_view
    return HTMLResponse(stage_view.render_dancers_html())
"""

# replace the existing /stage /tv block
import re
content = re.sub(r'@app\.get\("/stage", response_class=HTMLResponse\)\n@app\.get\("/tv", response_class=HTMLResponse\)\ndef stage_page\(\) -> HTMLResponse:.*?return HTMLResponse\(stage_view\.render_stage_html\(\)\)', html_endpoints, content, flags=re.DOTALL)

with open("src/karaoke/ctrl_api.py", "w") as f:
    f.write(content)
