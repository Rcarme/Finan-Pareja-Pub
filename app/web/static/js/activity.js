"use strict";
document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("click", event => {
    const chip = event.target.closest("[data-icon-target]");
    if (chip) {
      const input = document.getElementById(chip.dataset.iconTarget);
      if (input) {
        input.value = chip.dataset.iconValue;
        input.dataset.prevIcon = input.value;
        chip.closest(".emoji-picker-grid")?.querySelectorAll("[data-icon-target]").forEach(btn => {
          btn.setAttribute("aria-pressed", String(btn === chip));
        });
      }
      return;
    }
    const clearBtn = event.target.closest("[data-icon-clear]");
    if (clearBtn) {
      const input = document.getElementById(clearBtn.dataset.iconClear);
      if (input) {
        input.value = "";
        input.dataset.prevIcon = "";
        input.focus();
      }
    }
  });
  document.querySelectorAll('input[name="icon"]').forEach(input => {
    input.dataset.prevIcon = input.value;
    input.addEventListener("focus", () => {
      input.dataset.prevIcon = input.value;
    });
    input.addEventListener("input", () => {
      const prev = input.dataset.prevIcon || "";
      if (prev && input.value.length > prev.length && input.value.startsWith(prev)) {
        const added = input.value.slice(prev.length).trim();
        if (added) input.value = added;
      }
      input.dataset.prevIcon = input.value;
    });
  });
  const activity = document.getElementById("activity");
  if (!activity) return;
  const visibility = document.querySelector("[data-category-visibility]");
  const update = () => {
    const business = activity.value === "business";
    const kind = document.querySelector("[name=kind]:checked")?.value || document.querySelector("[name=kind]")?.value;
    document.querySelectorAll("[data-activity]").forEach(item => {
      const selected = item.tagName === "OPTION" ? item.selected : item.checked;
      const owner = item.dataset.categoryOwner;
      const visibleOwner = !visibility || visibility.checked || business || !owner || owner === visibility.dataset.currentUser || selected;
      const allowed = visibleOwner && business === (item.dataset.activity === "business") && (!kind || !item.dataset.kind || kind === item.dataset.kind);
      item.hidden = !allowed;
      item.disabled = !allowed;
      if (item.tagName === "OPTION") item.style.display = allowed ? "" : "none";
      if (!allowed && item.tagName === "OPTION" && item.selected) item.selected = false;
      if (!allowed && item.tagName === "INPUT") item.checked = false;
      if (item.tagName === "INPUT") item.closest("label").hidden = !allowed;
    });
    const topEnabled = document.querySelector(".category-picker > .category-grid input[name=category_id]:not([disabled])");
    if (business || visibility?.checked || !topEnabled) {
      const more = document.getElementById("more-categories");
      if (more) more.hidden = false;
      const button = document.querySelector("[data-toggle-categories]");
      if (button) button.setAttribute("aria-expanded", "true");
    }
    const enabledRadios = Array.from(document.querySelectorAll("input[type=radio][name=category_id]:not([disabled])"));
    if (enabledRadios.length === 1 && !enabledRadios.some(r => r.checked)) {
      enabledRadios[0].checked = true;
    }
    document.querySelectorAll("select").forEach(select => {
      if (!select.querySelector("option[data-activity]")) return;
      const current = select.options[select.selectedIndex];
      const enabledOptions = Array.from(select.querySelectorAll("option[data-activity]:not([disabled])"));
      if (!current || current.disabled) {
        const first = enabledOptions[0] || select.querySelector("option:not([disabled])");
        if (first) first.selected = true;
      } else if (!current.value && enabledOptions.length === 1) {
        enabledOptions[0].selected = true;
      }
    });
    const charged = document.getElementById("charged_to_user_id");
    if (charged) {
      charged.disabled = business;
      if (business) charged.value = "";
    }
  };
  activity.addEventListener("change", update);
  visibility?.addEventListener("change", update);
  document.querySelectorAll("[name=category_id]").forEach(input => input.addEventListener("change", update));
  document.querySelectorAll("[name=kind]").forEach(input => input.addEventListener("change", update));
  update();
});
