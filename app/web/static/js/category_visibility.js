"use strict";
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-category-visibility]").forEach(toggle => {
    const form = toggle.closest("form");
    if (!form) return;
    const update = () => {
      form.querySelectorAll("option[data-category-owner]").forEach(option => {
        const owner = option.dataset.categoryOwner;
        const visible = toggle.checked || !owner || owner === toggle.dataset.currentUser || option.selected;
        option.hidden = !visible;
        option.disabled = !visible;
      });
      form.querySelectorAll("select").forEach(select => {
        if (!select.querySelector("option[data-category-owner]")) return;
        const current = select.options[select.selectedIndex];
        if (!current || current.disabled) {
          const first = select.querySelector("option:not([disabled])");
          if (first) first.selected = true;
        }
      });
    };
    toggle.addEventListener("change", update);
    form.querySelectorAll("select[name^=category]").forEach(select => select.addEventListener("change", update));
    update();
  });
});
