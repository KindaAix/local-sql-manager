/* 视图面板：原生 JS，无依赖。开关抽屉、增删条件行、收集配置并提交。 */
(function () {
  "use strict";

  var panel = document.getElementById("view-panel");
  if (!panel) return;

  var dataEl = document.getElementById("view-panel-data");
  var meta = { fields: [], ops: {}, op_labels: {} };
  if (dataEl) {
    try {
      meta = JSON.parse(dataEl.textContent);
    } catch (error) {
      meta = { fields: [], ops: {}, op_labels: {} };
    }
  }

  var table = panel.getAttribute("data-table") || "";
  var rawId = panel.getAttribute("data-view") || "";
  var viewId = rawId ? parseInt(rawId, 10) : null;
  if (!viewId || isNaN(viewId)) viewId = null;

  var toggle = document.getElementById("view-panel-toggle");
  var closeButton = document.getElementById("vp-close");
  var errorBox = document.getElementById("vp-error");
  var nameInput = document.getElementById("vp-name");
  var filtersBox = document.getElementById("vp-filters");
  var sortsBox = document.getElementById("vp-sorts");
  var orderList = document.getElementById("vp-order");
  var groupSelect = document.getElementById("vp-group");

  function showError(message) {
    if (!errorBox) return;
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
  }

  function openPanel() {
    panel.hidden = false;
    showError("");
  }

  function closePanel() {
    panel.hidden = true;
    showError("");
  }

  if (toggle) toggle.addEventListener("click", function () {
    if (panel.hidden) openPanel();
    else closePanel();
  });

  document.querySelectorAll("[data-open-view-panel]").forEach(function (button) {
    button.addEventListener("click", function () {
      openPanel();
      if (nameInput) nameInput.focus();
    });
  });
  if (closeButton) closeButton.addEventListener("click", closePanel);

  function request(url, method, payload) {
    var options = { method: method, headers: {} };
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    return fetch(url, options).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          if (!response.ok) {
            throw new Error(data.error || "请求失败，请重试");
          }
          return data;
        });
    });
  }

  function make(tag, className) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function fieldType(column) {
    for (var index = 0; index < meta.fields.length; index += 1) {
      if (meta.fields[index].name === column) return meta.fields[index].type;
    }
    return "text";
  }

  function columnOptions(selected) {
    var options = [{ value: "", label: "（选择列）" }];
    meta.fields.forEach(function (field) {
      options.push({ value: field.name, label: field.name });
    });
    var select = make("select", "field-input vp-input");
    select.setAttribute("data-vp", "column");
    options.forEach(function (option) {
      var node = make("option");
      node.value = option.value;
      node.textContent = option.label;
      if (option.value === (selected || "")) node.selected = true;
      select.appendChild(node);
    });
    return select;
  }

  function opOptions(column, selected) {
    var ops = meta.ops[fieldType(column)] || meta.ops.text || [];
    var labels = meta.op_labels || {};
    var select = make("select", "field-input vp-input");
    select.setAttribute("data-vp", "op");
    ops.forEach(function (op) {
      var node = make("option");
      node.value = op;
      node.textContent = labels[op] || op;
      if (op === selected) node.selected = true;
      select.appendChild(node);
    });
    return select;
  }

  function rebuildOps(opSelect, column) {
    var current = opSelect.value;
    var opts = opOptions(column, current);
    opSelect.innerHTML = "";
    while (opts.firstChild) opSelect.appendChild(opts.firstChild);
  }

  function removeButton(label) {
    var button = make("button", "vp-remove");
    button.type = "button";
    button.textContent = "×";
    button.setAttribute("aria-label", label);
    return button;
  }

  function filterRow(item) {
    item = item || {};
    var row = make("div", "vp-row vp-filter");
    var column = columnOptions(item.column);
    var op = opOptions(item.column, item.op);
    var value = make("input", "field-input vp-input");
    value.type = "text";
    value.setAttribute("data-vp", "value");
    value.placeholder = "值";
    value.value = item.value === undefined || item.value === null ? "" : String(item.value);
    var remove = removeButton("删除条件");
    column.addEventListener("change", function () {
      rebuildOps(op, column.value);
    });
    remove.addEventListener("click", function () {
      row.remove();
    });
    row.appendChild(column);
    row.appendChild(op);
    row.appendChild(value);
    row.appendChild(remove);
    return row;
  }

  function sortRow(item) {
    item = item || {};
    var row = make("div", "vp-row vp-sort");
    var column = columnOptions(item.column);
    var dir = make("select", "field-input vp-input");
    dir.setAttribute("data-vp", "dir");
    [
      { value: "asc", label: "升序" },
      { value: "desc", label: "降序" }
    ].forEach(function (option) {
      var node = make("option");
      node.value = option.value;
      node.textContent = option.label;
      if (option.value === (item.dir || "asc")) node.selected = true;
      dir.appendChild(node);
    });
    var remove = removeButton("删除排序");
    remove.addEventListener("click", function () {
      row.remove();
    });
    row.appendChild(column);
    row.appendChild(dir);
    row.appendChild(remove);
    return row;
  }

  document.querySelectorAll("[data-add]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (button.getAttribute("data-add") === "filter") {
        if (filtersBox) filtersBox.appendChild(filterRow({}));
      } else if (sortsBox) {
        sortsBox.appendChild(sortRow({}));
      }
    });
  });

  if (orderList) {
    orderList.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-move]");
      if (!trigger) return;
      var item = trigger.closest(".vp-order-item");
      if (!item) return;
      if (trigger.getAttribute("data-move") === "up" && item.previousElementSibling) {
        orderList.insertBefore(item, item.previousElementSibling);
      } else if (
        trigger.getAttribute("data-move") === "down" &&
        item.nextElementSibling
      ) {
        orderList.insertBefore(item.nextElementSibling, item);
      }
    });
  }

  function collectConfig() {
    var filters = [];
    var sorts = [];
    var hidden = [];
    var order = [];
    var aggregates = [];

    document.querySelectorAll("#vp-filters .vp-filter").forEach(function (row) {
      var column = row.querySelector('[data-vp="column"]');
      if (!column || !column.value) return;
      var op = row.querySelector('[data-vp="op"]');
      var value = row.querySelector('[data-vp="value"]');
      if (!op || !op.value) return;
      filters.push({
        column: column.value,
        op: op.value,
        value: value ? value.value : ""
      });
    });

    document.querySelectorAll("#vp-sorts .vp-sort").forEach(function (row) {
      var column = row.querySelector('[data-vp="column"]');
      if (!column || !column.value) return;
      var dir = row.querySelector('[data-vp="dir"]');
      sorts.push({ column: column.value, dir: dir ? dir.value : "asc" });
    });

    document.querySelectorAll('[data-vp="hidden"]').forEach(function (box) {
      if (box.checked) hidden.push(box.value);
    });

    document.querySelectorAll("#vp-order .vp-order-item").forEach(function (item) {
      var column = item.getAttribute("data-column");
      if (column) order.push(column);
    });

    document.querySelectorAll("#vp-aggregates .vp-agg").forEach(function (row) {
      var fn = row.querySelector('[data-vp="fn"]');
      var column = row.querySelector('[data-vp="agg-column"]');
      if (!fn || !fn.value || !column || !column.value) return;
      aggregates.push({ column: column.value, fn: fn.value });
    });

    return {
      filters: filters,
      sorts: sorts,
      hidden: hidden,
      order: order,
      widths: {},
      group: groupSelect && groupSelect.value ? { column: groupSelect.value } : null,
      aggregates: aggregates
    };
  }

  function viewUrl(id) {
    var url = new URL(window.location.href);
    url.searchParams.set("view", id);
    return url.toString();
  }

  function reload() {
    window.location.reload();
  }

  function submit(saveAsNew) {
    showError("");
    var name = nameInput ? nameInput.value.trim() : "";
    if (!name) {
      showError("请填写视图名称");
      return;
    }
    var config = collectConfig();
    if (viewId && !saveAsNew) {
      request("/api/views/" + viewId, "POST", { name: name, config: config })
        .then(reload)
        .catch(function (error) {
          showError(error.message);
        });
      return;
    }
    request("/api/views", "POST", { table: table, name: name, config: config })
      .then(function (data) {
        window.location.href = viewUrl(data.id);
      })
      .catch(function (error) {
        showError(error.message);
      });
  }

  var saveButton = document.getElementById("vp-save");
  if (saveButton) saveButton.addEventListener("click", function () { submit(false); });
  var saveAsButton = document.getElementById("vp-save-as");
  if (saveAsButton) saveAsButton.addEventListener("click", function () { submit(true); });

  var copyButton = document.getElementById("vp-copy");
  if (copyButton) copyButton.addEventListener("click", function () {
    showError("");
    var base = nameInput ? nameInput.value.trim() : "";
    var name = (base || "视图") + " 副本";
    request("/api/views", "POST", { table: table, name: name, config: collectConfig() })
      .then(function (data) {
        window.location.href = viewUrl(data.id);
      })
      .catch(function (error) {
        showError(error.message);
      });
  });

  var defaultButton = document.getElementById("vp-default");
  if (defaultButton) defaultButton.addEventListener("click", function () {
    showError("");
    request("/api/views/" + viewId + "/default", "POST", {})
      .then(reload)
      .catch(function (error) {
        showError(error.message);
      });
  });

  var deleteButton = document.getElementById("vp-delete");
  if (deleteButton) deleteButton.addEventListener("click", function () {
    if (!window.confirm("删除这个视图后不可恢复，确定继续吗？")) return;
    showError("");
    request("/api/views/" + viewId, "DELETE")
      .then(reload)
      .catch(function (error) {
        showError(error.message);
      });
  });
})();
