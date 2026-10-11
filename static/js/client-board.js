(() => {
  const board = document.querySelector('[data-client-board]');
  if (!board) return;

  const boardError = document.querySelector('[data-client-board-error]');
  const status = document.querySelector('[data-client-board-status]');
  const csrfToken = document.querySelector('[data-client-board-csrf] [name=csrfmiddlewaretoken]')?.value;
  let draggedCard = null;
  let busy = false;

  const columnFor = (stage) => [...board.querySelectorAll('.client-board__column')]
    .find((column) => column.dataset.clientStatus === stage);
  const clearTargets = () => board.querySelectorAll('.client-board__column.is-drop-target').forEach(
    (column) => column.classList.remove('is-drop-target'),
  );
  const changeCount = (column, difference) => {
    const badge = column.querySelector('.client-board__count');
    const count = Math.max(0, Number(badge.textContent.trim()) + difference);
    badge.textContent = String(count);
    badge.setAttribute('aria-label', `Клиентов на этапе: ${count}`);
    return count;
  };
  const moveCard = (card, stage) => {
    const source = card.closest('.client-board__column');
    const destination = columnFor(stage);
    if (!source || !destination || source === destination) return;
    const sourceItems = source.querySelector('.client-board__items');
    const destinationItems = destination.querySelector('.client-board__items');
    destinationItems.querySelector('.client-board__empty')?.remove();
    destinationItems.prepend(card);
    card.dataset.clientStatus = stage;
    const remaining = changeCount(source, -1);
    changeCount(destination, 1);
    if (!sourceItems.querySelector('[data-client-card]')) {
      const empty = document.createElement('p');
      empty.className = 'client-board__empty';
      empty.textContent = remaining ? 'На этой странице клиентов нет' : 'Пока нет клиентов';
      sourceItems.append(empty);
    }
    status.textContent = 'Отношения с клиентом сохранены.';
    document.dispatchEvent(new Event('board-move-saved'));
  };
  const reconcileBoard = async (source, destination) => {
    const url = new URL(window.location.href);
    url.searchParams.delete(`p_${source}`);
    url.searchParams.delete(`p_${destination}`);
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
    const response = await fetch(url.toString(), { credentials: 'same-origin' });
    if (!response.ok) return;
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const freshBoard = page.querySelector('[data-client-board]');
    if (!freshBoard) return;
    board.innerHTML = freshBoard.innerHTML;
    columnFor(destination)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  };
  const changeStatus = async (card, target) => {
    if (busy) return;
    if (!csrfToken) {
      boardError.textContent = 'Не удалось проверить запрос. Обновите страницу и попробуйте снова.';
      boardError.classList.remove('d-none');
      return;
    }
    const source = card.dataset.clientStatus;
    boardError.classList.add('d-none');
    busy = true;
    status.textContent = 'Сохраняем отношения с клиентом…';
    try {
      const response = await fetch(card.dataset.statusUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-CSRFToken': csrfToken,
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: new URLSearchParams({ status: target, expected_status: source }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || result.status !== target) {
        throw new Error(result.error || 'Не удалось сохранить отношения. Обновите страницу и повторите перенос.');
      }
      moveCard(card, target);
      try {
        await reconcileBoard(source, target);
      } catch {
        // The saved card and counts are already updated; pagination can refresh later.
      }
    } catch (error) {
      boardError.textContent = error.message;
      boardError.classList.remove('d-none');
      status.textContent = error.message;
    } finally {
      busy = false;
    }
  };

  board.addEventListener('dragstart', (event) => {
    if (busy) { event.preventDefault(); return; }
    const card = event.target.closest('[data-client-card]');
    if (!card) return;
    draggedCard = card;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', card.dataset.statusUrl);
    card.classList.add('is-dragging');
  });
  board.addEventListener('dragover', (event) => {
    if (draggedCard) {
      const bounds = board.getBoundingClientRect();
      if (event.clientX < bounds.left + 48) board.scrollLeft -= 20;
      else if (event.clientX > bounds.right - 48) board.scrollLeft += 20;
    }
    const column = event.target.closest('.client-board__column');
    if (!draggedCard || !column || column.dataset.clientStatus === draggedCard.dataset.clientStatus) {
      clearTargets();
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    clearTargets();
    column.classList.add('is-drop-target');
  });
  board.addEventListener('drop', (event) => {
    const column = event.target.closest('.client-board__column');
    if (!draggedCard || !column) return;
    event.preventDefault();
    clearTargets();
    const card = draggedCard;
    draggedCard = null;
    card.classList.remove('is-dragging');
    if (column.dataset.clientStatus !== card.dataset.clientStatus) changeStatus(card, column.dataset.clientStatus);
  });
  board.addEventListener('dragend', () => {
    draggedCard?.classList.remove('is-dragging');
    draggedCard = null;
    clearTargets();
  });
  board.addEventListener('dragleave', (event) => {
    if (!board.contains(event.relatedTarget)) clearTargets();
  });
})();
