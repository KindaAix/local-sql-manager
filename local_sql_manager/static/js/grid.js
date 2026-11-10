/* 网格交互：选中、编辑、粘贴、批量删除、新增行、列宽与列序拖拽。
 *
 * 原生 JS，无框架、无构建、无外部依赖。所有写请求都走 /api 下的 JSON 接口，
 * 服务层保证一次请求 = 一条可撤销操作；乐观锁版本取自行的 data-updated-at，
 * 编辑成功后用返回片段中的新值继续下一次保存。
 */
(function () {
  "use strict";

  var grid = document.querySelector("table.grid");
  if (!grid) return;

  var tbody = grid.tBodies[0];
  var theadRow = grid.tHead ? grid.tHead.rows[0] : null;
  if (!tbody || !theadRow) return;

  var table = tableFromPath();
  if (!table) return;

  var viewId = viewIdFromQuery();
  var readOnlyPage = !!document.querySelector(".badge-readonly");
  var JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json"
  };

  var AUX_COLUMNS = { created_at: true, updated_at: true };

  var selected = null;
  var editing = null;
  var writesBlocked = false;
  var deleteButton = null;
  var widthTimer = null;
  var chain = Promise.resolve();

  /* ------------------------------------------------------------------ *
   * 基础工具
   * ------------------------------------------------------------------ */

  function tableFromPath() {
    var match = location.pathname.match(/^\/t\/(.+?)\/?$/);
    if (!match) return "";
    try {
      return decodeURIComponent(match[1]);
    } catch (error) {
      return match[1];
    }
  }

  function viewIdFromQuery() {
    var raw = new URLSearchParams(location.search).get("view");
    return raw && /^\d+$/.test(raw) ? raw : null;
  }

  function apiUrl(kind) {
    var path = table.split("/").map(encodeURIComponent).join("/");
    var url = "/api/t/" + path + "/" + kind;
    if (viewId) url += "?view=" + encodeURIComponent(viewId);
    return url;
  }

  function dataRows() {
    return Array.prototype.slice.call(tbody.querySelectorAll("tr.grid-row"));
  }

  function dataCells(tr) {
    if (!tr) return [];
    return Array.prototype.slice.call(tr.querySelectorAll("td.cell[data-col]"));
  }

  function rowById(id) {
    var wanted = String(id);
    return dataRows().filter(function (tr) {
      return tr.dataset.id === wanted;
    })[0] || null;
  }

  function columnHeads() {
    return Array.prototype.slice
      .call(theadRow.querySelectorAll("th.grid-head"))
      .filter(function (th) {
        return !th.classList.contains("grid-head-select");
      });
  }

  function cellFor(tr, column) {
    return dataCells(tr).filter(function (cell) {
      return cell.dataset.col === column;
    })[0] || null;
  }

  function isEditable(cell) {
    return !!cell && cell.dataset.readonly !== "1";
  }

  function enqueue(task) {
    var run = chain.then(task, task);
    chain = run.catch(function () {});
    return run;
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(payload === undefined ? {} : payload)
    });
  }

  function errorMessage(response) {
    return response
      .json()
      .then(function (data) {
        if (data && data.error) return data.error;
        return fallbackMessage(response);
      })
      .catch(function () {
        return fallbackMessage(response);
      });
  }

  function fallbackMessage(response) {
    if (response.status === 415) return "请求格式必须是 JSON";
    if (response.status === 404) return "目标不存在，可能已被删除";
    if (response.status === 409) return "数据已被修改，请重新加载";
    return "操作失败，请重试";
  }

  /* ------------------------------------------------------------------ *
   * 提示与冲突遮罩
   * ------------------------------------------------------------------ */

  function showToast(message, actionLabel, onAction) {
    var toast = document.createElement("div");
    toast.className = "toast";
    var text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = message;
    toast.appendChild(text);
    if (actionLabel) {
      var action = document.createElement("button");
      action.type = "button";
      action.className = "toast-action";
      action.textContent = actionLabel;
      action.addEventListener("click", function () {
        toast.remove();
        if (onAction) onAction();
      });
      toast.appendChild(action);
    }
    document.body.appendChild(toast);
    setTimeout(function () {
      toast.remove();
    }, actionLabel ? 8000 : 4000);
  }

  function conflictReload() {
    if (writesBlocked) return;
    writesBlocked = true;
    if (editing) {
      var cell = editing.cell;
      editing = null;
      teardownEdit(cell);
    }
    updateDeleteButton();
    var overlay = document.createElement("div");
    overlay.className = "conflict-overlay";
    var box = document.createElement("div");
    box.className = "conflict-box";
    box.textContent = "数据已被修改，正在重新加载";
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    setTimeout(function () {
      location.reload();
    }, 2000);
  }

  /* ------------------------------------------------------------------ *
   * 选择与键盘移动
   * ------------------------------------------------------------------ */

  function selectCell(cell, keepScroll) {
    if (!cell || selected === cell) return;
    if (selected) selected.classList.remove("selected");
    selected = cell;
    cell.classList.add("selected");
    if (!keepScroll) {
      cell.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function moveSelection(direction) {
    if (!selected || !document.body.contains(selected)) return;
    var tr = selected.closest("tr");
    var cells = dataCells(tr);
    var index = cells.indexOf(selected);
    if (index < 0) return;

    if (direction === "down" || direction === "up") {
      var rows = dataRows();
      var rowIndex = rows.indexOf(tr);
      var targetRow = rows[rowIndex + (direction === "down" ? 1 : -1)];
      if (!targetRow) return;
      var targetCells = dataCells(targetRow);
      var target = targetCells[Math.min(index, targetCells.length - 1)];
      if (target) selectCell(target);
      return;
    }

    var next = cells[index + (direction === "right" ? 1 : -1)];
    if (next) selectCell(next);
  }

  /* ------------------------------------------------------------------ *
   * 单元格编辑
   * ------------------------------------------------------------------ */

  function beginEdit(cell, initialValue) {
    if (readOnlyPage || writesBlocked) return;
    if (!isEditable(cell)) return;
    if (editing) {
      commitEdit(null);
    }
    var original = cell.textContent;
    var input = document.createElement("input");
    input.type = "text";
    input.className = "cell-editor";
    input.value = initialValue === undefined ? original : initialValue;
    cell.classList.add("is-editing");
    cell.appendChild(input);
    editing = { cell: cell, input: input, original: original };
    selectCell(cell, true);
    input.focus();
    var end = input.value.length;
    input.setSelectionRange(end, end);
    input.addEventListener("keydown", onEditorKeydown);
    input.addEventListener("blur", onEditorBlur);
  }

  function teardownEdit(cell) {
    if (!cell) return;
    cell.classList.remove("is-editing");
    var input = cell.querySelector("input.cell-editor");
    if (input) input.remove();
  }

  function onEditorKeydown(event) {
    if (event.key === "Enter") {
      event.preventDefault();
      commitEdit("down");
    } else if (event.key === "Tab") {
      event.preventDefault();
      commitEdit("right");
    } else if (event.key === "Escape") {
      event.preventDefault();
      if (editing) {
        var cell = editing.cell;
        editing = null;
        teardownEdit(cell);
        selectCell(cell, true);
      }
    }
  }

  function onEditorBlur() {
    if (!editing) return;
    commitEdit(null);
  }

  function commitEdit(direction) {
    if (!editing) return;
    var cell = editing.cell;
    var input = editing.input;
    var original = editing.original;
    var value = input.value;
    editing = null;
    teardownEdit(cell);

    var tr = cell.closest("tr");
    if (!tr || !tr.dataset.id) return;
    var rowId = tr.dataset.id;
    var column = cell.dataset.col;
    var wasSelected = selected === cell;

    if (value === original) {
      if (direction) moveSelection(direction);
      return;
    }

    enqueue(function () {
      return saveChanges(makeChanges([[rowId, column, value]])).then(function (ok) {
        if (!ok) return;
        // 失焦保存时用户可能已经点到别的单元格，别把选择抢回来。
        if (wasSelected) restoreSelection(rowId, column);
        if (direction) moveSelection(direction);
      });
    });
  }

  function makeChanges(entries) {
    var changes = {};
    entries.forEach(function (entry) {
      var rowId = entry[0];
      var column = entry[1];
      var value = entry[2];
      if (!changes[rowId]) changes[rowId] = {};
      changes[rowId][column] = value;
    });
    return changes;
  }

  function restoreSelection(rowId, column) {
    var tr = rowById(rowId);
    if (!tr) return;
    var cell = cellFor(tr, column);
    if (cell) selectCell(cell, true);
  }

  /* ------------------------------------------------------------------ *
   * 保存（含行片段替换）
   * ------------------------------------------------------------------ */

  function saveChanges(changes) {
    if (writesBlocked) return Promise.resolve(false);
    var expected = {};
    var ids = Object.keys(changes);
    var missing = false;
    ids.forEach(function (id) {
      var tr = rowById(id);
      if (!tr) {
        missing = true;
        return;
      }
      var version = tr.dataset.updatedAt || "";
      if (!version) {
        missing = true;
        return;
      }
      expected[id] = version;
    });
    if (missing) {
      showToast("缺少版本信息，请刷新页面后重试");
      return Promise.resolve(false);
    }

    return fetch(apiUrl("save"), {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "text/html" },
      body: JSON.stringify({ changes: changes, expected: expected })
    }).then(function (response) {
      if (response.ok) {
        return response.text().then(function (html) {
          applyFragments(html);
          return true;
        });
      }
      if (response.status === 409) {
        conflictReload();
        return false;
      }
      return errorMessage(response).then(function (message) {
        showToast(message);
        return false;
      });
    }).catch(function () {
      showToast("网络异常，修改未保存");
      return false;
    });
  }

  function applyFragments(html) {
    var temp = document.createElement("tbody");
    temp.innerHTML = html.trim();
    var newRows = Array.prototype.slice.call(
      temp.querySelectorAll("tr.grid-row")
    );
    if (!newRows.length) return;
    newRows.forEach(function (fresh) {
      var old = rowById(fresh.dataset.id);
      if (old) {
        old.replaceWith(fresh);
      } else {
        var addRow = tbody.querySelector("tr.grid-add-row");
        tbody.insertBefore(fresh, addRow || null);
      }
      decorateRow(fresh);
    });
  }

  /* ------------------------------------------------------------------ *
   * 粘贴
   * ------------------------------------------------------------------ */

  function pasteMatrix(text) {
    var normalized = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    var lines = normalized.split("\n");
    while (lines.length && lines[lines.length - 1] === "") lines.pop();
    if (!lines.length) return [];
    return lines.map(function (line) {
      return line.split("\t");
    });
  }

  function pasteIntoGrid(text) {
    if (!selected || !document.body.contains(selected)) return;
    if (readOnlyPage || writesBlocked) return;
    var matrix = pasteMatrix(text);
    if (!matrix.length) return;

    var startRow = selected.closest("tr");
    var startCells = dataCells(startRow);
    var startColumn = startCells.indexOf(selected);
    if (startColumn < 0) return;
    var allRows = dataRows();
    var startRowIndex = allRows.indexOf(startRow);

    var entries = [];
    for (var r = 0; r < matrix.length; r += 1) {
      var tr = allRows[startRowIndex + r];
      if (!tr || !tr.dataset.id) break;
      var cells = dataCells(tr);
      for (var c = 0; c < matrix[r].length; c += 1) {
        var cell = cells[startColumn + c];
        if (!cell || !isEditable(cell)) continue;
        var value = matrix[r][c];
        if (value === cell.textContent) continue;
        entries.push([tr.dataset.id, cell.dataset.col, value]);
      }
    }
    if (!entries.length) {
      showToast("没有可粘贴的单元格");
      return;
    }

    var anchorRow = startRow.dataset.id;
    var anchorColumn = selected.dataset.col;
    enqueue(function () {
      return saveChanges(makeChanges(entries)).then(function (ok) {
        if (!ok) return;
        restoreSelection(anchorRow, anchorColumn);
        if (matrix.length > 1 || matrix[0].length > 1) {
          showToast("已粘贴 " + matrix.length + " 行");
        }
      });
    });
  }

  /* ------------------------------------------------------------------ *
   * 批量选择与删除
   * ------------------------------------------------------------------ */

  function decorateRow(tr) {
    if (!tr || tr.querySelector("td.cell-select")) return;
    var holder = document.createElement("td");
    holder.className = "cell cell-select";
    var box = document.createElement("input");
    box.type = "checkbox";
    box.className = "row-check";
    box.addEventListener("change", updateDeleteButton);
    holder.appendChild(box);
    tr.insertBefore(holder, tr.firstChild);
  }

  function injectSelectionColumn() {
    if (theadRow.querySelector("th.grid-head-select")) return;
    var th = document.createElement("th");
    th.className = "grid-head grid-head-select";
    th.scope = "col";
    var box = document.createElement("input");
    box.type = "checkbox";
    box.className = "select-all";
    box.title = "全选本页";
    box.addEventListener("change", function () {
      dataRows().forEach(function (tr) {
        var check = tr.querySelector(".row-check");
        if (check) check.checked = box.checked;
      });
      updateDeleteButton();
    });
    th.appendChild(box);
    theadRow.insertBefore(th, theadRow.firstChild);

    tbody.querySelectorAll("td.group-cell, td.empty-cell").forEach(function (td) {
      td.colSpan = td.colSpan + 1;
    });
    dataRows().forEach(decorateRow);
  }

  function checkedRows() {
    return dataRows().filter(function (tr) {
      var check = tr.querySelector(".row-check");
      return !!check && check.checked;
    });
  }

  function updateDeleteButton() {
    var count = checkedRows().length;
    if (deleteButton) {
      deleteButton.disabled = count === 0 || readOnlyPage || writesBlocked;
      deleteButton.textContent = count
        ? "删除选中（" + count + "）"
        : "删除选中";
    }
    var all = theadRow.querySelector(".select-all");
    if (all) {
      var rows = dataRows();
      all.checked = rows.length > 0 && count === rows.length;
      all.indeterminate = count > 0 && count < rows.length;
    }
  }

  function deleteSelected() {
    if (readOnlyPage || writesBlocked) return;
    var rows = checkedRows();
    if (!rows.length) return;
    var ids = rows
      .map(function (tr) {
        return tr.dataset.id;
      })
      .filter(Boolean);

    postJson(apiUrl("delete"), { ids: ids })
      .then(function (response) {
        if (!response.ok) {
          return errorMessage(response).then(showToast);
        }
        return response.json().then(function (data) {
          rows.forEach(function (tr) {
            var check = tr.querySelector(".row-check");
            if (check) check.checked = false;
            tr.remove();
          });
          updateDeleteButton();
          if (data.deleted > 0) {
            showToast("已删除 " + data.deleted + " 条", "撤销", function () {
              undoOperation(data.op);
            });
          } else {
            showToast("所选行已在回收站中");
          }
        });
      })
      .catch(function () {
        showToast("网络异常，删除失败");
      });
  }

  function undoOperation(opId) {
    if (!opId) return;
    postJson("/api/undo/" + encodeURIComponent(opId), {})
      .then(function (response) {
        if (!response.ok) {
          return errorMessage(response).then(showToast);
        }
        showToast("已撤销");
        setTimeout(function () {
          location.reload();
        }, 400);
      })
      .catch(function () {
        showToast("网络异常，撤销失败");
      });
  }

  /* ------------------------------------------------------------------ *
   * 新增行
   * ------------------------------------------------------------------ */

  function addRow() {
    if (readOnlyPage || writesBlocked) return;
    postJson(apiUrl("insert"), { values: {} })
      .then(function (response) {
        if (!response.ok) {
          return errorMessage(response).then(showToast);
        }
        return response.text().then(function (html) {
          var temp = document.createElement("tbody");
          temp.innerHTML = html.trim();
          var fresh = temp.querySelector("tr.grid-row");
          if (!fresh) {
            showToast("新增失败，请重试");
            return;
          }
          var empty = tbody.querySelector("tr.empty-row");
          if (empty) empty.remove();
          var anchor = tbody.querySelector("tr.grid-add-row");
          tbody.insertBefore(fresh, anchor || null);
          decorateRow(fresh);
          updateDeleteButton();
          var cell = dataCells(fresh).filter(isEditable)[0];
          if (cell) selectCell(cell);
        });
      })
      .catch(function () {
        showToast("网络异常，新增失败");
      });
  }

  function injectAddRow() {
    if (tbody.querySelector("tr.grid-add-row")) return;
    var tr = document.createElement("tr");
    tr.className = "grid-add-row";
    var holder = document.createElement("td");
    holder.className = "grid-add-cell";
    holder.colSpan = columnHeads().length + 1;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "grid-add-button";
    button.textContent = "＋ 新增行";
    button.addEventListener("click", addRow);
    holder.appendChild(button);
    tr.appendChild(holder);
    tbody.appendChild(tr);
  }

  /* ------------------------------------------------------------------ *
   * 列宽拖拽
   * ------------------------------------------------------------------ */

  function setupResizers() {
    columnHeads().forEach(function (th) {
      var grip = document.createElement("span");
      grip.className = "col-resizer";
      grip.title = "拖动调整列宽";
      grip.addEventListener("pointerdown", function (event) {
        startResize(event, th);
      });
      th.appendChild(grip);
    });
  }

  function startResize(event, th) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    var startX = event.clientX;
    var startWidth = th.getBoundingClientRect().width;
    var started = false;

    function fixLayout() {
      // 浏览器默认按内容分配列宽；第一次真的移动时固定当前像素值，避免整表跳动。
      var widths = columnHeads().map(function (head) {
        return Math.round(head.getBoundingClientRect().width);
      });
      grid.classList.add("grid-fixed");
      columnHeads().forEach(function (head, index) {
        head.style.width = widths[index] + "px";
      });
    }

    function onMove(moveEvent) {
      if (!started) {
        started = true;
        fixLayout();
      }
      var width = Math.max(
        40,
        Math.min(800, Math.round(startWidth + moveEvent.clientX - startX))
      );
      th.style.width = width + "px";
    }

    function onUp() {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onUp);
      if (started) scheduleWidthSave();
    }

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onUp);
  }

  function scheduleWidthSave() {
    clearTimeout(widthTimer);
    widthTimer = setTimeout(saveWidths, 400);
  }

  function saveWidths() {
    var widths = {};
    columnHeads().forEach(function (th) {
      if (AUX_COLUMNS[th.dataset.col]) return;
      var width = parseInt(th.style.width, 10);
      if (!width) width = Math.round(th.getBoundingClientRect().width);
      widths[th.dataset.col] = width;
    });
    if (!viewId) {
      showToast("新建视图后可保存列宽");
      return;
    }
    postJson("/api/views/" + encodeURIComponent(viewId) + "/config", {
      widths: widths
    })
      .then(function (response) {
        if (!response.ok) {
          return errorMessage(response).then(showToast);
        }
      })
      .catch(function () {
        showToast("网络异常，列宽未保存");
      });
  }

  /* ------------------------------------------------------------------ *
   * 列序拖拽
   * ------------------------------------------------------------------ */

  function setupHeaderDrag() {
    columnHeads().forEach(function (th) {
      th.addEventListener("pointerdown", function (event) {
        if (event.button !== 0) return;
        if (event.target.closest(".col-resizer")) return;
        startHeaderDrag(event, th);
      });
    });
  }

  function startHeaderDrag(event, th) {
    var startX = event.clientX;
    var startY = event.clientY;
    var dragging = false;

    function onMove(moveEvent) {
      if (!dragging) {
        var distance =
          Math.abs(moveEvent.clientX - startX) +
          Math.abs(moveEvent.clientY - startY);
        if (distance < 4) return;
        dragging = true;
        th.classList.add("is-dragging");
      }
    }

    function onUp(upEvent) {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      th.classList.remove("is-dragging");
      if (!dragging) return;
      var target = document.elementFromPoint(upEvent.clientX, upEvent.clientY);
      var targetTh = target && target.closest ? target.closest("th.grid-head") : null;
      if (
        !targetTh ||
        targetTh === th ||
        targetTh.classList.contains("grid-head-select")
      ) {
        return;
      }
      var source = th.dataset.col;
      var heads = columnHeads();
      var order = heads.map(function (head) {
        return head.dataset.col;
      });
      var from = order.indexOf(source);
      var to = order.indexOf(targetTh.dataset.col);
      if (from < 0 || to < 0) return;
      order.splice(from, 1);
      order.splice(to, 0, source);
      applyOrder(order);
      saveOrder(order);
    }

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  }

  function applyOrder(order) {
    var headMap = {};
    columnHeads().forEach(function (th) {
      headMap[th.dataset.col] = th;
    });
    order.forEach(function (name) {
      if (headMap[name]) theadRow.appendChild(headMap[name]);
    });

    dataRows().forEach(function (tr) {
      var cellMap = {};
      dataCells(tr).forEach(function (cell) {
        cellMap[cell.dataset.col] = cell;
      });
      order.forEach(function (name) {
        if (cellMap[name]) tr.appendChild(cellMap[name]);
      });
    });
  }

  function saveOrder(order) {
    var business = order.filter(function (name) {
      return !AUX_COLUMNS[name];
    });
    if (!viewId) {
      showToast("新建视图后可保存列序");
      return;
    }
    postJson("/api/views/" + encodeURIComponent(viewId) + "/config", {
      order: business
    })
      .then(function (response) {
        if (!response.ok) {
          return errorMessage(response).then(showToast);
        }
      })
      .catch(function () {
        showToast("网络异常，列序未保存");
      });
  }

  /* ------------------------------------------------------------------ *
   * 事件绑定与初始化
   * ------------------------------------------------------------------ */

  function bindEvents() {
    tbody.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      if (target.closest("input, button")) return;
      var cell = target.closest("td.cell[data-col]");
      if (!cell || !grid.contains(cell)) return;
      if (editing && editing.cell !== cell) return; // 让编辑框先失焦保存
      selectCell(cell);
    });

    tbody.addEventListener("dblclick", function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      if (target.closest("input, button")) return;
      var cell = target.closest("td.cell[data-col]");
      if (!cell || cell.dataset.readonly === "1") return;
      beginEdit(cell, cell.textContent);
    });

    document.addEventListener("keydown", function (event) {
      if (editing) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      var target = event.target;
      if (target && target.closest && target.closest("input, textarea, select")) {
        return;
      }
      if (!selected || !document.body.contains(selected)) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        moveSelection("down");
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        moveSelection("up");
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        moveSelection("left");
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        moveSelection("right");
      } else if (event.key === "Enter") {
        if (isEditable(selected)) {
          event.preventDefault();
          beginEdit(selected, selected.textContent);
        }
      } else if (event.key.length === 1 && event.key !== " ") {
        if (isEditable(selected)) {
          event.preventDefault();
          beginEdit(selected, selected.textContent + event.key);
        }
      }
    });

    document.addEventListener("paste", function (event) {
      if (editing) return;
      if (!selected || !document.body.contains(selected)) return;
      if (readOnlyPage || writesBlocked) return;
      var data = event.clipboardData || window.clipboardData;
      if (!data) return;
      var text = data.getData("text/plain");
      if (!text) return;
      event.preventDefault();
      pasteIntoGrid(text);
    });
  }

  function injectToolbar() {
    var actions = document.querySelector(".topbar-actions");
    if (!actions) return;
    deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "button button-ghost grid-delete";
    deleteButton.textContent = "删除选中";
    deleteButton.disabled = true;
    deleteButton.addEventListener("click", deleteSelected);
    actions.appendChild(deleteButton);
  }

  function syncHeadMeta() {
    columnHeads().forEach(function (th) {
      th.dataset.col = th.textContent.trim();
    });
  }

  syncHeadMeta();
  if (!readOnlyPage) {
    injectSelectionColumn();
    injectToolbar();
    injectAddRow();
  }
  setupResizers();
  setupHeaderDrag();
  bindEvents();
  updateDeleteButton();
})();
