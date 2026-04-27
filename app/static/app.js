/* =========================================================
   SHOPIFY APP BRIDGE
========================================================= */
import createApp from "@shopify/app-bridge";
import { getSessionToken } from "@shopify/app-bridge-utils";

const app = createApp({
  apiKey: window.SHOPIFY_API_KEY,
  host: new URLSearchParams(window.location.search).get("host"),
});


/* =========================================================
   TOAST UI
========================================================= */
const toastWrap = document.createElement("div");
toastWrap.className = "toast-wrap";
document.body.appendChild(toastWrap);

function toast(message) {
  const el = document
    .querySelector("#toast-template")
    .content.firstElementChild.cloneNode(true);

  el.textContent = message;
  toastWrap.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}


/* =========================================================
   AUTH + CSRF
========================================================= */
async function getAuthHeaders() {
  const sessionToken = await getSessionToken(app);

  const res = await fetch("/api/csrf", {
    headers: {
      Authorization: `Bearer ${sessionToken}`,
    },
  });

  if (!res.ok) throw new Error("CSRF fetch failed");

  const data = await res.json();

  return {
    Authorization: `Bearer ${sessionToken}`,
    "X-CSRF-Token": data.csrf_token,
  };
}


/* =========================================================
   FETCH SICURO
========================================================= */
async function secureFetch(url, options = {}) {
  const authHeaders = await getAuthHeaders();

  const response = await fetch(url, {
    ...options,
    headers: {
      ...(options.headers || {}),
      ...authHeaders,
    },
  });

  let payload = {};
  try {
    payload = await response.json();
  } catch { }

  if (!response.ok) {
    throw new Error(payload.detail || "Errore richiesta");
  }

  return payload;
}


/* =========================================================
   UPLOAD (con progress)
========================================================= */
async function uploadFiles(zone, fileList) {
  if (!fileList.length) return;

  const input = zone.querySelector("[data-file-input]");
  const progress = zone.querySelector("[data-progress]");
  const progressBar = zone.querySelector("[data-progress-bar]");

  const form = new FormData();
  form.append("product_id", zone.dataset.productId);
  [...fileList].forEach(file => form.append("files", file));

  progress.hidden = false;
  progressBar.style.width = "10%";

  const headers = await getAuthHeaders();

  try {
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();

      xhr.open("POST", "/api/upload");

      // 🔐 HEADERS SICUREZZA
      Object.entries(headers).forEach(([key, value]) => {
        xhr.setRequestHeader(key, value);
      });

      xhr.upload.onprogress = (evt) => {
        if (!evt.lengthComputable) return;
        const pct = Math.round((evt.loaded / evt.total) * 100);
        progressBar.style.width = `${pct}%`;
      };

      xhr.onload = () => {
        try {
          const data = JSON.parse(xhr.responseText || "{}");
          if (xhr.status >= 200 && xhr.status < 300) resolve(data);
          else reject(new Error(data.detail || "Upload failed"));
        } catch {
          reject(new Error("Upload failed"));
        }
      };

      xhr.onerror = () => reject(new Error("Network error"));

      xhr.send(form);
    });

    progressBar.style.width = "100%";
    toast("Upload completato");
    setTimeout(() => window.location.reload(), 400);

  } catch (err) {
    toast(err.message || "Errore upload");
    progress.hidden = true;
    progressBar.style.width = "0%";
  }
}


/* =========================================================
   DROPZONE
========================================================= */
function bindDropzone(zone) {
  const input = zone.querySelector("[data-file-input]");

  zone.addEventListener("click", () => input.click());

  input.addEventListener("change", () => {
    uploadFiles(zone, input.files);
  });

  ["dragenter", "dragover"].forEach(type => {
    zone.addEventListener(type, (e) => {
      e.preventDefault();
      zone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach(type => {
    zone.addEventListener(type, (e) => {
      e.preventDefault();
      zone.classList.remove("dragover");
    });
  });

  zone.addEventListener("drop", (e) => {
    if (e.dataTransfer?.files?.length) {
      uploadFiles(zone, e.dataTransfer.files);
    }
  });
}


/* =========================================================
   GALLERY / MEDIA ACTIONS
========================================================= */
async function sendForm(url, data) {
  try {
    await secureFetch(url, {
      method: "POST",
      body: data,
    });
    return true;
  } catch (err) {
    toast(err.message || "Errore");
    return false;
  }
}


/* =========================================================
   INIT
========================================================= */
document.querySelectorAll("[data-upload-dropzone]").forEach(bindDropzone);

document.querySelectorAll("[data-add-gallery]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const form = new FormData();
    form.append("product_id", btn.dataset.productId);
    form.append("file_id", btn.dataset.fileId);

    if (await sendForm("/api/gallery/add", form)) {
      toast("Aggiunto alla gallery");
      setTimeout(() => window.location.reload(), 300);
    }
  });
});

document.querySelectorAll("[data-remove-gallery]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const form = new FormData();
    form.append("product_id", btn.dataset.productId);
    form.append("file_id", btn.dataset.fileId);

    if (await sendForm("/api/gallery/remove", form)) {
      toast("Rimosso dalla gallery");
      setTimeout(() => window.location.reload(), 300);
    }
  });
});

document.querySelectorAll("[data-delete-media]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (!confirm("Eliminare questa immagine dal prodotto?")) return;

    const form = new FormData();
    form.append("product_id", btn.dataset.productId);
    form.append("media_id", btn.dataset.mediaId);

    if (await sendForm("/api/media/delete", form)) {
      toast("Immagine eliminata");
      setTimeout(() => window.location.reload(), 300);
    }
  });
});