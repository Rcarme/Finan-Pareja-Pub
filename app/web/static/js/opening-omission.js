"use strict";
(() => {
    const fieldset = document.querySelector("[data-opening-date]");
    if (!fieldset) return;
    const checkbox = fieldset.querySelector("input");
    const date = document.getElementById("tx_date");
    const period = document.getElementById("period");
    const update = () => {
        const kind = document.querySelector('input[name="kind"]:checked')?.value || document.querySelector('input[name="kind"]')?.value;
        const eligible = kind === "expense" && date.value && date.value < fieldset.dataset.openingDate;
        fieldset.hidden = !eligible;
        checkbox.disabled = !eligible;
    };
    date.addEventListener("change", update);
    document.querySelectorAll('input[name="kind"]').forEach(input => input.addEventListener("change", update));
    checkbox.addEventListener("change", () => {
        if (period && checkbox.checked && period.value < fieldset.dataset.openingDate.slice(0, 7)) {
            period.value = fieldset.dataset.openingDate.slice(0, 7);
        }
    });
    update();
})();
