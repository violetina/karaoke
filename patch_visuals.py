import sys

with open("src/karaoke/visuals.py", "r") as f:
    lines = f.readlines()

# Find start and end
start_idx = -1
end_idx = -1
for i, line in enumerate(lines):
    if line.startswith("def dance_floor_frame("):
        start_idx = i
    if line.startswith("def animate_mood_pixels("):
        end_idx = i
        break

if start_idx == -1 or end_idx == -1:
    print("Could not find function bounds!")
    sys.exit(1)

# Remove the old function and any blank lines before animate_mood_pixels
while end_idx > start_idx and lines[end_idx-1].strip() == "":
    end_idx -= 1

new_func = """def dance_floor_frame(
    bpm: float | None,
    elapsed: float,
    *,
    width: int = 80,
    height: int = 12,
    num_dancers: int = 4,
    mood: str = "neutral",
    genre: str = "",
    energy: float | None = None,
    chord_cpm: float | None = None,
    fifth_ratio: float | None = None,
) -> str:
    \"\"\"Render a two-tier dance floor (background + main stage) with ASCII dancers.\"\"\"
    import math
    safe_bpm = float(bpm or 90.0)
    beat = 60.0 / max(safe_bpm, 1.0)
    beats = elapsed / beat
    e = max(0.0, min(1.0, float(energy if energy is not None else 0.5)))
    
    # Analyze chords for styling (is it jazz/chordy?)
    is_jazz = False
    if fifth_ratio is not None and chord_cpm is not None:
        if fifth_ratio > 0.4 and chord_cpm > 10:
            is_jazz = True

    # Main stage pose set
    genre_text = (genre or "").casefold()
    if any(w in genre_text for w in ("metal", "hardcore", "punk", "rock")):
        pose_indices = [0, 1, 10, 6, 7, 2, 3, 8]  # headbanging, kicks
        floor_char = "✷"
    elif any(w in genre_text for w in ("electronic", "techno", "dance", "house", "edm")):
        pose_indices = [1, 8, 9, 4, 5, 0, 3, 2]  # waves, jumps
        floor_char = "·"
    elif any(w in genre_text for w in ("hip hop", "hip-hop", "rap", "soul")):
        pose_indices = [4, 11, 5, 0, 2, 3, 1, 6]  # disco, dab
        floor_char = "~"
    elif mood == "tender":
        pose_indices = [0, 2, 3, 1, 0, 3, 2, 1]  # gentle sways
        floor_char = "♡"
    else:
        pose_indices = [0, 1, 2, 3, 4, 6, 7, 8]
        floor_char = "·"

    # If it's jazz, swap poses to something groovy and snap-like
    if is_jazz:
        pose_indices = [4, 9, 5, 0, 2, 3, 1, 6]
        floor_char = "♬"
        
    speed = 1.0 + e * 0.8
    
    # 2-tier setup: background vs main stage
    bg_dancers_count = max(4, int(width / 8))  # A lot of dancers in the back
    fg_dancers_count = max(1, min(12, num_dancers))
    
    # Dimensions
    figure_w = 5
    fg_slot_w = max(figure_w, width // max(1, fg_dancers_count))
    bg_slot_w = max(figure_w, width // max(1, bg_dancers_count))
    
    total_dancer_h = 3
    canvas_h = max(total_dancer_h * 2 + 2, min(height, total_dancer_h * 2 + 4))
    
    def render_layer(d_count, slot_w, is_bg=False):
        layer_canvas = [[" "] * width for _ in range(total_dancer_h)]
        for d in range(d_count):
            phase_offset = d * 1.618033988749895
            
            # BG is slower
            d_beats = beats * (speed * 0.5 if is_bg else speed) + phase_offset
            
            if is_bg:
                # Randomish poses for BG
                pidx = int(d_beats) % len(_DANCE_POSES)
                pose = _DANCE_POSES[pidx]
            else:
                pidx = int(d_beats * 2) % len(pose_indices)
                pose = _DANCE_POSES[pose_indices[pidx]]
            
            is_airborne = False
            if not is_bg and safe_bpm >= 100:
                within = d_beats % 1.0
                is_airborne = (within < 0.2)
            
            col = d * slot_w + (slot_w - figure_w) // 2
            
            for y in range(total_dancer_h):
                src_y = y
                if is_airborne:
                    src_y = y - 1
                if 0 <= src_y < total_dancer_h:
                    line = pose[src_y]
                    for x in range(min(len(line), figure_w)):
                        if col + x < width:
                            layer_canvas[y][col + x] = line[x]
                            
        # Join into strings, add markup if bg
        res = []
        for y in range(total_dancer_h):
            row_str = "".join(layer_canvas[y])
            if is_bg:
                row_str = f"[dim]{row_str}[/dim]"
            res.append(row_str)
        return res
        
    bg_rows = render_layer(bg_dancers_count, bg_slot_w, is_bg=True)
    fg_rows = render_layer(fg_dancers_count, fg_slot_w, is_bg=False)
    
    # Assemble the floor
    floor_line = floor_char * width
    if is_jazz:
        floor_line = f"[bold magenta]{floor_line}[/bold magenta]"
    
    out_lines = []
    gap_between = canvas_h - (total_dancer_h * 2 + 1)
    
    out_lines.extend(bg_rows)
    for _ in range(max(0, gap_between)):
        out_lines.append(" " * width)
    out_lines.extend(fg_rows)
    out_lines.append(floor_line)
    
    while len(out_lines) < canvas_h:
        out_lines.append(" " * width)
        
    status_parts = [f"{safe_bpm:.0f} BPM"]
    if is_jazz:
        status_parts.append("Jazz Style (Chords!)")
    if mood != "neutral":
        status_parts.append(mood)
        
    out_lines.append(" · ".join(status_parts))
    return "\\n".join(out_lines)

"""

new_lines = lines[:start_idx] + [new_func + "\n\n"] + lines[end_idx:]

with open("src/karaoke/visuals.py", "w") as f:
    f.writelines(new_lines)
