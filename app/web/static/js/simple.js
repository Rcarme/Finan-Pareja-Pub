"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const more = document.querySelector("[data-toggle-categories]");
  const categories = document.getElementById("more-categories");
  if (more && categories) {
    if (categories.querySelector("input:checked")) {
      categories.hidden = false;
      more.setAttribute("aria-expanded", "true");
    }
    categories.addEventListener("invalid", () => {
      categories.hidden = false;
      more.setAttribute("aria-expanded", "true");
    }, true);
    more.addEventListener("click", () => {
      categories.hidden = !categories.hidden;
      more.setAttribute("aria-expanded", String(!categories.hidden));
    });
  }
  const chart = document.getElementById("simple-category-chart");
  if (chart && window.Chart) {
    const amounts = JSON.parse(document.getElementById("simple-chart-data").textContent);
    const labels = JSON.parse(document.getElementById("simple-chart-labels").textContent);
    const links = JSON.parse(document.getElementById("simple-chart-links")?.textContent || "[]");
    const colors = [...document.querySelectorAll(".legend-dot")].map(element => getComputedStyle(element).color);
    new Chart(chart, {type: "doughnut", data: {labels, datasets: [{data: amounts.map(Number), backgroundColor: colors, borderWidth: 0}]}, options: {responsive: true, maintainAspectRatio: false, cutout: "72%", onClick: (_event, elements) => {const href = links[elements[0]?.index]; if (href?.startsWith("/s/list?")) window.location.assign(href);}, plugins: {legend: {display: false}}}});
  }
  document.querySelectorAll("form[data-undo-seconds]").forEach(form => {
    const deadline = Date.now() + Number(form.dataset.undoSeconds) * 1000;
    const output = form.querySelector("[data-undo-countdown]");
    const timer = setInterval(() => {
      const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      output.textContent = remaining;
      if (!remaining) {form.hidden = true; clearInterval(timer);}
    }, 1000);
  });
  document.querySelectorAll("form[method='post']").forEach(form => {
    form.addEventListener("submit", event => {
      if (form.dataset.submitting) {event.preventDefault(); return;}
      if (!form.checkValidity()) return;
      form.dataset.submitting = "true";
      form.querySelectorAll("button[type='submit']").forEach(button => {
        button.disabled = true;
        button.dataset.originalText = button.textContent;
        button.textContent = button.dataset.submittingText || "Guardando…";
      });
    });
  });
});
window.addEventListener("pageshow", () => {
  document.querySelectorAll("form[data-submitting]").forEach(form => {
    delete form.dataset.submitting;
    form.querySelectorAll("button[data-original-text]").forEach(button => {button.disabled = false; button.textContent = button.dataset.originalText;});
  });
});
