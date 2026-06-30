# -*- coding: utf-8 -*-
"""Full-screen viewer for the parking layout image uploaded on room.office.

Browsers show a raw /web/image URL left-aligned on a default background,
which looks unpolished. This controller wraps the image in a minimal dark
page with click-to-zoom and drag-to-pan, suitable for client demos.
"""
from markupsafe import Markup

from odoo import http
from odoo.http import request


class ParkingMapController(http.Controller):

    @http.route('/parking/map/<int:office_id>', type='http', auth='user')
    def map_viewer(self, office_id, **kwargs):
        office = request.env['room.office'].browse(office_id).exists()
        if not office:
            return request.not_found()
        # Read-access check (raises if user can't see this record)
        office.check_access('read')
        if not office.image_1920:
            return request.not_found()

        timestamp = int(office.write_date.timestamp()) if office.write_date else 0
        image_url = f'/web/image/room.office/{office_id}/image_1920?unique={timestamp}'
        title = office.display_name or 'Parking Map'

        # Inline template - no QWeb, keeps the page self-contained and fast.
        html = PAGE_TEMPLATE.format(
            title=Markup.escape(title),
            image_url=Markup.escape(image_url),
        )
        return request.make_response(
            html,
            headers=[('Content-Type', 'text/html; charset=utf-8')],
        )


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Parking Map - {title}</title>
<style>
  html, body {{
    margin: 0; padding: 0;
    background: #0b1220; color: #e2e8f0;
    height: 100vh; width: 100vw; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  header {{
    position: fixed; top: 0; left: 0; right: 0;
    padding: 10px 20px;
    background: rgba(11, 18, 32, 0.85);
    backdrop-filter: blur(10px);
    display: flex; justify-content: space-between; align-items: center;
    z-index: 10;
    border-bottom: 1px solid rgba(148, 163, 184, 0.15);
  }}
  header h1 {{ margin: 0; font-size: 15px; font-weight: 500; }}
  header .hint {{ font-size: 12px; color: #94a3b8; }}
  .viewport {{
    position: absolute; inset: 0;
    overflow: auto;
    display: flex; align-items: center; justify-content: center;
    padding: 60px 20px 20px;
    box-sizing: border-box;
  }}
  .viewport.panning {{ cursor: grabbing; }}
  .viewport img {{
    display: block;
    border: 1px solid #1e293b;
    border-radius: 6px;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6);
    user-select: none;
    -webkit-user-drag: none;
    cursor: zoom-in;
    transition: max-width 0.2s ease, max-height 0.2s ease;
  }}
  .viewport.fit img {{
    max-width: calc(100vw - 40px);
    max-height: calc(100vh - 80px);
  }}
  .viewport.full img {{
    max-width: none; max-height: none;
    cursor: zoom-out;
  }}
</style>
</head>
<body>
<header>
  <h1>Parking Map &middot; {title}</h1>
  <div class="hint">Click image to toggle fit / 100% &nbsp;&middot;&nbsp; Drag to pan when zoomed</div>
</header>
<div class="viewport fit" id="viewport">
  <img id="map" src="{image_url}" alt="Parking Map"/>
</div>
<script>
(function () {{
  var viewport = document.getElementById('viewport');
  var img = document.getElementById('map');

  img.addEventListener('click', function (e) {{
    e.stopPropagation();
    if (viewport.classList.contains('fit')) {{
      viewport.classList.remove('fit');
      viewport.classList.add('full');
    }} else {{
      viewport.classList.remove('full');
      viewport.classList.add('fit');
    }}
  }});

  // Drag-to-pan when zoomed beyond the viewport
  var isDown = false, startX = 0, startY = 0, scrollL = 0, scrollT = 0;
  viewport.addEventListener('mousedown', function (e) {{
    if (e.target !== img) return;
    isDown = true;
    viewport.classList.add('panning');
    startX = e.pageX; startY = e.pageY;
    scrollL = viewport.scrollLeft; scrollT = viewport.scrollTop;
  }});
  window.addEventListener('mouseup', function () {{
    isDown = false;
    viewport.classList.remove('panning');
  }});
  window.addEventListener('mousemove', function (e) {{
    if (!isDown) return;
    e.preventDefault();
    viewport.scrollLeft = scrollL - (e.pageX - startX);
    viewport.scrollTop = scrollT - (e.pageY - startY);
  }});
}})();
</script>
</body>
</html>
"""
