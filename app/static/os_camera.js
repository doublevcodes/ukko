// The Live camera panel: the shelf video, the clickable product boxes drawn over it, and the
// sensor readings underneath.
// The video is Stockpot's relay of the camera (GET /camera/feed). The boxes come from GET /state
// (`detections`): the vision loop on the laptop asks Gemini where each watched product stands.

const EMPTY_VIEWPORT = `<div class="viewport-empty"><b>Camera feed not connected yet</b><span>This space is reserved for the live shelf view.</span></div>`;
let camera = 0;            // which camera tab is showing
let mountedFeed = null;    // the feed address now in the slot, so the MJPEG stream is not reconnected on every poll
let shownDetections = [];  // the boxes last drawn, to place them again once the picture's size is known

function drawCamera(state) {
  const chosen = BASELINE.cameras[camera];
  put("camera-tabs", BASELINE.cameras.map((entry, index) =>
    `<button class="tab ${index === camera ? "on" : ""}" data-camera="${index}">${esc(entry.label)}</button>`).join(""));
  const reporting = chosen.watchesShelf
    ? state.shelf.filter((item) => item.updated_at).sort((a, b) => b.updated_at.localeCompare(a.updated_at)) : [];
  put("camera-tag", reporting.length ? pill("live", `${reporting[0].device_id} reporting`) : pill("", "No signal"));
  put("camera-telemetry", !chosen.watchesShelf ? `<p class="small">No camera is connected here yet.</p>` : state.shelf.map((item) => `
    <div class="reading ${picked(item)}" data-product-key="${esc(item.product_key)}"><span class="dot ${shelfStatus(item)}"></span><span class="grow" title="${esc(item.name)}">${esc(item.name)}</span>
      <b>${item.level ? LEVEL_WORDS[item.level] : "Not seen yet"}</b>
      <span class="small">${item.updated_at ? `${percent(item.confidence)} conf. · ${clock(item.updated_at)} · ${esc(item.device_id)}` : "waiting for the camera"}</span>
    </div>`).join(""));
  mountFeed(chosen.feedUrl && (state.health || {}).camera_connected ? chosen.feedUrl : null);
  drawOverlay(chosen.watchesShelf ? state.detections || [] : []);
}

// Show the video while the camera is sending, and the empty viewport while it is not.
function mountFeed(feedUrl) {
  if (feedUrl === mountedFeed) return;
  mountedFeed = feedUrl;
  const slot = $("live-camera-slot");
  if (!feedUrl) { slot.innerHTML = EMPTY_VIEWPORT; return; }
  const picture = document.createElement("img");
  picture.alt = "Live video of the watched shelf";
  // The first frame tells the picture's real size, which the boxes need.
  picture.onload = () => drawOverlay(shownDetections);
  // If the feed cannot be opened, show the empty viewport; the next poll tries again.
  picture.onerror = () => { mountedFeed = null; slot.innerHTML = EMPTY_VIEWPORT; };
  picture.src = feedUrl;
  slot.replaceChildren(picture);
}

// Where a box, given as fractions of the whole camera frame, lands in the viewport (in percent).
// The picture fills the viewport and its overflow is cropped (object-fit: cover), so the frame is
// scaled by the larger of the two ratios and centred. Returns null for a box that is cropped away.
function placeBox(box, frame, view) {
  const scale = Math.max(view.width / frame.width, view.height / frame.height);
  const width = frame.width * scale, height = frame.height * scale;
  const left = (view.width - width) / 2, top = (view.height - height) / 2;
  const within = (value, limit) => Math.min(limit, Math.max(0, value));
  const x1 = within(left + box.x * width, view.width), x2 = within(left + (box.x + box.w) * width, view.width);
  const y1 = within(top + box.y * height, view.height), y2 = within(top + (box.y + box.h) * height, view.height);
  if (x2 - x1 < 8 || y2 - y1 < 8) return null;
  return { left: x1 / view.width * 100, top: y1 / view.height * 100, width: (x2 - x1) / view.width * 100, height: (y2 - y1) / view.height * 100 };
}

// One button per detected product. The buttons are kept and moved rather than rebuilt, so a box
// under the pointer stays hovered and glides to its new place when the next detection arrives.
function drawOverlay(detections) {
  shownDetections = detections;
  const overlay = $("camera-overlay"), picture = $("live-camera-slot").querySelector("img");
  const view = { width: overlay.clientWidth, height: overlay.clientHeight };
  const frame = picture && picture.naturalWidth ? { width: picture.naturalWidth, height: picture.naturalHeight } : view;
  const boxes = !view.width ? [] : detections.map((found) => ({ ...found, place: placeBox(found.box, frame, view) }))
    .filter((found) => found.place)
    .sort((a, b) => a.product_key.localeCompare(b.product_key) || a.box.x - b.box.x);
  while (overlay.children.length > boxes.length) overlay.lastChild.remove();
  while (overlay.children.length < boxes.length) {
    const hit = document.createElement("button");
    hit.type = "button"; hit.className = "hit";
    overlay.append(hit);
  }
  boxes.forEach((found, index) => {
    const hit = overlay.children[index], on = found.product_key === selectedProductKey;
    for (const side of ["left", "top", "width", "height"]) hit.style[side] = `${found.place[side].toFixed(2)}%`;
    // A smaller box sits above a larger one, so a product standing in front of another stays clickable.
    hit.style.zIndex = String(1000 - Math.round(found.place.width * found.place.height / 10));
    hit.dataset.productKey = found.product_key;
    hit.title = found.name;
    hit.setAttribute("aria-label", `Select ${found.name}`);
    hit.setAttribute("aria-pressed", String(on));
    hit.classList.toggle("selected", on);
  });
}
