const form = document.querySelector("#login-form");
const message = document.querySelector("#login-message");
const palettes = [
  ["靛蓝", "#4f46e5", "#3730a3"], ["海蓝", "#2563eb", "#1d4ed8"],
  ["青色", "#0891b2", "#0e7490"], ["翡翠", "#059669", "#047857"],
  ["青柠", "#65a30d", "#4d7c0f"], ["琥珀", "#d97706", "#b45309"],
  ["珊瑚", "#ea580c", "#c2410c"], ["玫红", "#e11d48", "#be123c"],
  ["紫罗兰", "#7c3aed", "#6d28d9"], ["洋红", "#c026d3", "#a21caf"],
];

function applyPalette(index) {
  const palette = palettes[index] || palettes[0];
  document.documentElement.style.setProperty("--primary", palette[1]);
  document.documentElement.style.setProperty("--primary2", palette[2]);
  document.documentElement.style.setProperty("--primary-soft", `${palette[1]}26`);
}

const tools = document.createElement("div");
tools.className = "login-tools";
tools.innerHTML = '<button id="login-theme" class="icon-button" title="切换深浅主题" aria-label="切换深浅主题"></button><button id="login-palette" class="icon-button" title="切换配色" aria-label="切换配色"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="9" cy="9" r="1"/><circle cx="15" cy="8" r="1"/><circle cx="16" cy="14" r="1"/><path d="M8 16h2a2 2 0 0 1 2 2v2"/></svg></button>';
document.body.append(tools);

const themeButton = document.querySelector("#login-theme");
function syncThemeIcon() {
  themeButton.innerHTML = document.documentElement.dataset.theme === "dark"
    ? '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M20 15A8 8 0 0 1 9 4a9 9 0 1 0 11 11"/></svg>';
}

applyPalette(Number(localStorage.getItem("cleanllm-palette")) || 0);
syncThemeIcon();
themeButton.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("cleanllm-theme", next);
  syncThemeIcon();
});

document.querySelector("#login-palette").addEventListener("click", (event) => {
  event.stopPropagation();
  const existing = document.querySelector("#login-palette-menu");
  if (existing) { existing.remove(); return; }
  const menu = document.createElement("div");
  menu.id = "login-palette-menu";
  menu.className = "palette-menu login-palette-menu";
  palettes.forEach((palette, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "palette-item";
    const dot = document.createElement("i");
    dot.className = "palette-dot";
    dot.style.background = palette[1];
    button.append(dot, document.createTextNode(palette[0]));
    button.addEventListener("click", () => {
      applyPalette(index);
      localStorage.setItem("cleanllm-palette", String(index));
      menu.remove();
    });
    menu.append(button);
  });
  document.body.append(menu);
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#login-palette-menu") && !event.target.closest("#login-palette")) {
    document.querySelector("#login-palette-menu")?.remove();
  }
});

async function initializeAppearance() {
  try {
    const response = await fetch("/api/appearance", {cache: "no-store"});
    if (!response.ok) return;
    const data = await response.json();
    if (typeof data.background === "string" && data.background.startsWith("/api/appearance/background/")) {
      const image = new Image();
      image.src = data.background;
      await Promise.race([
        image.decode?.().catch(() => undefined) || Promise.resolve(),
        new Promise((resolve) => setTimeout(resolve, 700)),
      ]);
      document.querySelector(".login-view").style.setProperty(
        "background-image",
        `linear-gradient(rgba(11,13,18,.5),rgba(11,13,18,.72)),url("${data.background}")`,
        "important",
      );
    }
  } catch (_) {
    // The form remains usable with the default background.
  } finally {
    document.body.style.visibility = "visible";
  }
}
initializeAppearance();
setTimeout(() => { document.body.style.visibility = "visible"; }, 900);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  message.textContent = "";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        username: document.querySelector("#username").value,
        password: document.querySelector("#password").value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "登录失败");
    location.assign("/");
  } catch (error) {
    message.textContent = error.message === "Failed to fetch" ? "无法连接服务，请稍后重试" : error.message;
  } finally {
    button.disabled = false;
  }
});
