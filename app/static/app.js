const toastWrap = document.createElement("div");
toastWrap.className = "toast-wrap";
document.body.appendChild(toastWrap);

function toast(message) {
  const el = document.querySelector("#toast-template").content.firstElementChild.cloneNode(true);
  el.textContent = message;
  toastWrap.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

async function postForm(url, data) {
  const response = await fetch(url, { method: "POST", body: data });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail ? JSON.stringify(payload.detail) : "Request failed");
  }
  return payload;
}

function bindDropzone(zone) {
  const input = zone.querySelector("[data-file-input]");
  const progress = zone.querySelector("[data-progress]");
  const progressBar = zone.querySelector("[data-progress-bar]");

  function openPicker() {
    input.click();
  }

  async function uploadFiles(fileList) {
    if (!fileList.length) return;
    const form = new FormData();
    form.append("shop", zone.dataset.shop);
    form.append("product_id", zone.dataset.productId);
    [...fileList].forEach(file => form.append("files", file));

    progress.hidden = false;
    progressBar.style.width = "15%";

    try {
      const xhrPromise = new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/upload");
        xhr.upload.onprogress = (evt) => {
          if (!evt.lengthComputable) return;
          const pct = Math.round((evt.loaded / evt.total) * 100);
          progressBar.style.width = `${pct}%`;
        };
        xhr.onload = () => {
          try {
            const data = JSON.parse(xhr.responseText || "{}");
            if (xhr.status >= 200 && xhr.status < 300) resolve(data);
            else reject(new Error(data.detail ? JSON.stringify(data.detail) : "Upload failed"));
          } catch {
            reject(new Error("Upload failed"));
          }
        };
        xhr.onerror = () => reject(new Error("Network error"));
        xhr.send(form);
      });

      await xhrPromise;
      progressBar.style.width = "100%";
      toast("Upload completato");
      setTimeout(() => window.location.reload(), 400);
    } catch (error) {
      toast(error.message || "Errore upload");
      progress.hidden = true;
      progressBar.style.width = "0%";
    }
  }

  zone.addEventListener("click", openPicker);
  input.addEventListener("change", () => uploadFiles(input.files));

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
      uploadFiles(e.dataTransfer.files);
    }
  });
}

document.querySelectorAll("[data-upload-dropzone]").forEach(bindDropzone);

document.querySelectorAll("[data-add-gallery]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const form = new FormData();
    form.append("shop", btn.dataset.shop);
    form.append("product_id", btn.dataset.productId);
    form.append("file_id", btn.dataset.fileId);
    try {
      await postForm("/api/gallery/add", form);
      toast("Aggiunto alla gallery");
      setTimeout(() => window.location.reload(), 300);
    } catch (error) {
      toast(error.message || "Errore");
    }
  });
});

document.querySelectorAll("[data-remove-gallery]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const form = new FormData();
    form.append("shop", btn.dataset.shop);
    form.append("product_id", btn.dataset.productId);
    form.append("file_id", btn.dataset.fileId);
    try {
      await postForm("/api/gallery/remove", form);
      toast("Rimosso dalla gallery");
      setTimeout(() => window.location.reload(), 300);
    } catch (error) {
      toast(error.message || "Errore");
    }
  });
});

document.querySelectorAll("[data-delete-media]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (!confirm("Eliminare questa immagine dal prodotto?")) return;
    const form = new FormData();
    form.append("shop", btn.dataset.shop);
    form.append("product_id", btn.dataset.productId);
    form.append("media_id", btn.dataset.mediaId);
    try {
      await postForm("/api/media/delete", form);
      toast("Immagine eliminata");
      setTimeout(() => window.location.reload(), 300);
    } catch (error) {
      toast(error.message || "Errore");
    }
  });
});
