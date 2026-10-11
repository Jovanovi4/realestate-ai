(() => {
  const board = document.querySelector('[data-deal-board]');
  const modalElement = document.getElementById('deal-loss-modal');
  if (!board || !modalElement || !window.bootstrap) return;

  const modal = bootstrap.Modal.getOrCreateInstance(modalElement);
  const note = modalElement.querySelector('[data-deal-loss-note]');
  const confirm = modalElement.querySelector('[data-deal-loss-confirm]');
  const modalError = modalElement.querySelector('[data-deal-loss-error]');
  const boardError = document.querySelector('[data-deal-board-error]');
  const status = document.querySelector('[data-deal-board-status]');
  const activeCount = document.querySelector('[data-deal-active-count]');
  const csrfToken = document.querySelector('[data-deal-board-csrf] [name=csrfmiddlewaretoken]')?.value;
  let draggedCard = null;
  let pendingCard = null;
  let busy = false;

  const columnFor = (stage) => [...board.querySelectorAll('.deal-board__column')]
    .find((column) => column.dataset.dealStage === stage);
  const clearTargets = () => board.querySelectorAll('.deal-board__column.is-drop-target').forEach(
    (column) => column.classList.remove('is-drop-target'),
  );
  const clearErrors = () => {
    boardError.classList.add('d-none');
    modalError.classList.add('d-none');
    note.classList.remove('is-invalid');
  };
  const showError = (message, inModal = false) => {
    const target = inModal ? modalError : boardError;
    target.textContent = message;
    target.classList.remove('d-none');
    status.textContent = message;
  };
  const openLossModal = (card) => {
    pendingCard = card;
    note.value = '';
    clearErrors();
    modal.show();
  };
  const changeCount = (column, difference) => {
    const badge = column.querySelector('.client-board__count');
    const count = Math.max(0, Number(badge.textContent.trim()) + difference);
    badge.textContent = String(count);
    badge.setAttribute('aria-label', `Сделок на этапе: ${count}`);
    return count;
  };
  const moveCard = (card, stage) => {
    const source = card.closest('.deal-board__column');
    const destination = columnFor(stage);
    if (!source || !destination || source === destination) return;
    const sourceItems = source.querySelector('.client-board__items');
    const destinationItems = destination.querySelector('.client-board__items');
    destinationItems.querySelector('.client-board__empty')?.remove();
    destinationItems.prepend(card);
    card.dataset.dealStage = stage;
    const remaining = changeCount(source, -1);
    changeCount(destination, 1);
    if (!sourceItems.querySelector('[data-deal-card]')) {
      const empty = document.createElement('p');
      empty.className = 'client-board__empty';
      empty.textContent = remaining ? 'На этой странице сделок нет' : 'Пока нет сделок';
      sourceItems.append(empty);
    }
    const filteredStage = board.classList.contains('deal-board--analytics-filtered');
    const activeDifference = filteredStage ? -1 : Number(destination.dataset.activeStage === 'true')
      - Number(source.dataset.activeStage === 'true');
    const totalActive = Math.max(0, Number(activeCount.dataset.count) + activeDifference);
    activeCount.dataset.count = String(totalActive);
    activeCount.textContent = `${filteredStage ? 'Сделок на этапе' : 'Активных сделок'}: ${totalActive}`;
    status.textContent = 'Этап сделки сохранён.';
    document.dispatchEvent(new Event('board-move-saved'));
  };
  const reconcileBoard = async (stage) => {
    const url = new URL(window.location.href);
    url.searchParams.delete(`p_${stage}`);
    window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
    const response = await fetch(url.toString(), { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) return;
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const freshBoard = page.querySelector('[data-deal-board]');
    if (!freshBoard) return;
    board.innerHTML = freshBoard.innerHTML;
    document.dispatchEvent(new Event('deal-board-refreshed'));
    const freshActiveCount = page.querySelector('[data-deal-active-count]');
    if (freshActiveCount) {
      activeCount.dataset.count = freshActiveCount.dataset.count;
      activeCount.textContent = freshActiveCount.textContent;
    }
    columnFor(stage)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  };
  const changeStage = async (card, stage, outcomeNote = '', inModal = false) => {
    if (busy) return;
    if (!csrfToken) {
      showError('Не удалось проверить запрос. Обновите страницу и попробуйте снова.', inModal);
      return;
    }
    clearErrors();
    busy = true;
    confirm.disabled = true;
    status.textContent = 'Сохраняем этап сделки…';
    try {
      const body = new URLSearchParams({
        stage,
        expected_stage: card.dataset.dealStage,
        outcome_note: outcomeNote,
      });
      const response = await fetch(card.dataset.stageUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-CSRFToken': csrfToken,
          'X-Requested-With': 'XMLHttpRequest',
        },
        body,
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || result.stage !== stage) {
        throw new Error(result.error || 'Не удалось сохранить этап. Обновите страницу и попробуйте снова.');
      }
      if (inModal) modal.hide();
      moveCard(card, stage);
      try {
        await reconcileBoard(stage);
      } catch {
        // The saved card and counts are already updated; pagination can refresh later.
      }
    } catch (error) {
      showError(error.message, inModal);
    } finally {
      busy = false;
      confirm.disabled = false;
    }
  };

  document.addEventListener('deal-panel-stage-saved', (event) => {
    const { id, stage } = event.detail || {};
    if (!stage || !columnFor(stage)) return;
    const card = [...board.querySelectorAll('[data-deal-card]')].find((item) => item.dataset.dealId === String(id));
    if (card) moveCard(card, stage);
    if (window.matchMedia('(max-width: 767.98px)').matches) {
      const picker = document.querySelector('[data-board-stage-picker]');
      if (picker) {
        picker.value = stage;
        picker.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    reconcileBoard(stage).catch(() => {
      if (!card) showError('Этап сохранён. Обновите страницу, чтобы увидеть сделку в колонке.');
    });
  });

  board.addEventListener('dragstart', (event) => {
    if (busy) {
      event.preventDefault();
      return;
    }
    const card = event.target.closest('[data-deal-card]');
    if (!card) return;
    draggedCard = card;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', card.dataset.stageUrl);
    card.classList.add('is-dragging');
  });
  board.addEventListener('dragover', (event) => {
    if (draggedCard) {
      const bounds = board.getBoundingClientRect();
      if (event.clientX < bounds.left + 48) board.scrollLeft -= 20;
      else if (event.clientX > bounds.right - 48) board.scrollLeft += 20;
    }
    const column = event.target.closest('.deal-board__column');
    if (!draggedCard || !column || column.dataset.dealStage === draggedCard.dataset.dealStage) {
      clearTargets();
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    clearTargets();
    column.classList.add('is-drop-target');
  });
  board.addEventListener('drop', (event) => {
    const column = event.target.closest('.deal-board__column');
    if (!draggedCard || !column) return;
    event.preventDefault();
    clearTargets();
    const card = draggedCard;
    draggedCard = null;
    card.classList.remove('is-dragging');
    const stage = column.dataset.dealStage;
    if (stage === card.dataset.dealStage) return;
    if (stage === 'lost') openLossModal(card);
    else changeStage(card, stage);
  });
  board.addEventListener('dragend', () => {
    draggedCard?.classList.remove('is-dragging');
    draggedCard = null;
    clearTargets();
  });
  board.addEventListener('dragleave', (event) => {
    if (!board.contains(event.relatedTarget)) clearTargets();
  });
  modalElement.addEventListener('hidden.bs.modal', () => {
    pendingCard = null;
    clearErrors();
  });
  confirm.addEventListener('click', () => {
    if (!pendingCard || busy) return;
    const outcomeNote = note.value.trim();
    if (!outcomeNote) {
      note.classList.add('is-invalid');
      note.focus();
      return;
    }
    changeStage(pendingCard, 'lost', outcomeNote, true);
  });
})();
