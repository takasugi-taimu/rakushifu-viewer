let currentDate = new Date();

const calendarContainer = document.getElementById('calendarContainer');
const detailModal = new bootstrap.Modal(document.getElementById('detailModal'));
const calendarPage = document.getElementById('calendarPage');
const searchPage = document.getElementById('searchPage');
const payPage = document.getElementById('payPage');
const detailModalElement = document.getElementById('detailModal');
let modalRequestId = 0;
let modalView = null;
let selectedWorkerButton = null;
let shiftScrollTop = 0;

function setModalView(view) {
    modalView = view;
    const isStaff = view === 'staff';
    document.getElementById('shiftDetailHeader').hidden = isStaff;
    document.getElementById('modalBody').hidden = isStaff;
    document.getElementById('staffDetailHeader').hidden = !isStaff;
    document.getElementById('staffDetailBody').hidden = !isStaff;
    document.getElementById('backToShiftBtn').hidden = !isStaff || staffDetailSource !== 'shift';
    const titleId = isStaff ? 'staffDetailName' : 'modalTitle';
    detailModalElement.setAttribute('aria-labelledby', titleId);
    if (detailModalElement.classList.contains('show')) document.getElementById(titleId).focus();
}

async function authenticatedFetch(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('Login required');
    }
    return response;
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (character) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[character]);
}

detailModalElement.addEventListener('hide.bs.modal', () => {
    modalRequestId++;
    modalView = null;
    currentStaffData = null;
    selectedWorkerButton = null;
});

detailModalElement.addEventListener('shown.bs.modal', () => {
    document.getElementById(modalView === 'staff' ? 'staffDetailName' : 'modalTitle').focus();
});

const appHeader = document.getElementById('appHeader');
window.addEventListener('scroll', () => {
    appHeader.classList.toggle('scrolled', window.scrollY > 4);
}, { passive: true });

function updateMonthDisplay() {
    const year = currentDate.getFullYear();
    const month = currentDate.getMonth();
    document.getElementById('monthYear').textContent = `${year}年`;
    document.getElementById('monthNum').textContent = month + 1;
    document.getElementById('mobileMonth').textContent = `${year}年${month + 1}月`;
}

async function fetchCalendarData(year, month) {
    try {
        const response = await authenticatedFetch(`/api/calendar?year=${year}&month=${month + 1}`);
        if (!response.ok) throw new Error('API Error');
        return await response.json();
    } catch (error) {
        console.error('Failed to fetch calendar data:', error);
        return null;
    }
}

let calendarRequestId = 0;
let calendarRefreshTimer;
const configuredCacheSeconds = Number(document.body.dataset.cacheSeconds);
const calendarRefreshMs = (Number.isFinite(configuredCacheSeconds) && configuredCacheSeconds >= 0
    ? configuredCacheSeconds : 120) * 1000 + 1000;

async function renderCalendar(silent = false) {
    const requestId = ++calendarRequestId;
    clearTimeout(calendarRefreshTimer);
    const year = currentDate.getFullYear();
    const month = currentDate.getMonth();

    updateMonthDisplay();

    if (!silent) {
        document.getElementById('mobileShiftSummary').textContent = '自分の勤務日を確認中…';
        calendarContainer.innerHTML = `
            <div class="loading-placeholder">
                <div class="spinner-ring"></div>
                <span class="loading-text">読み込み中</span>
            </div>`;
    }

    const shiftData = await fetchCalendarData(year, month);
    if (requestId !== calendarRequestId) return;
    if (shiftData === null) {
        if (!silent) document.getElementById('mobileShiftSummary').textContent = '自分の勤務日を取得できませんでした';
        if (!silent) calendarContainer.innerHTML = '<div class="loading-placeholder">シフトを取得できませんでした</div>';
        calendarRefreshTimer = setTimeout(() => renderCalendar(true), Math.min(calendarRefreshMs, 30000));
        return;
    }
    const cells = document.createDocumentFragment();

    const firstDay = new Date(year, month, 1);
    const lastDay = new Date(year, month + 1, 0);
    const startDay = firstDay.getDay();
    const totalDays = lastDay.getDate();
    const today = new Date();
    let myShiftDays = 0;

    for (let i = 0; i < startDay; i++) {
        const emptyCell = document.createElement('div');
        emptyCell.className = 'calendar-day empty';
        cells.appendChild(emptyCell);
    }

    for (let day = 1; day <= totalDays; day++) {
        const dateObj = new Date(year, month, day);
        const dateStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const dayOfWeek = dateObj.getDay();

        const dayData = shiftData[dateStr] || { has_me: false, total_count: 0 };
        if (dayData.has_me) myShiftDays++;
        const isToday = dateObj.getDate() === today.getDate() &&
                        dateObj.getMonth() === today.getMonth() &&
                        dateObj.getFullYear() === today.getFullYear();

        const cell = document.createElement('div');
        let classes = 'calendar-day animate-in';
        if (dayData.has_me) classes += ' has-my-shift';
        if (isToday) classes += ' is-today';
        if (dayOfWeek === 0) classes += ' day-sun';
        if (dayOfWeek === 6) classes += ' day-sat';
        cell.className = classes;
        cell.style.animationDelay = `${(day - 1) * 18}ms`;

        let html = `<span class="date-num">${day}</span>`;

        if (dayData.has_me) {
            const startTime = dayData.my_shift_time || '';
            const endTime = dayData.my_shift_end_time || '';
            html += `<div class="shift-badge mine"><span class="shift-t">${escapeHtml(startTime)}</span><span class="shift-sep">~</span><span class="shift-t">${escapeHtml(endTime)}</span></div>`;
        }

        const otherCount = dayData.total_count - (dayData.has_me ? 1 : 0);
        if (otherCount > 0 && !dayData.has_me) {
            html += `<div class="shift-badge other">${otherCount}人</div>`;
        }

        cell.innerHTML = html;
        cell.onclick = () => showDetail(dateStr);
        cells.appendChild(cell);
    }
    calendarContainer.replaceChildren(cells);
    document.getElementById('mobileShiftSummary').textContent = `自分の勤務日：${myShiftDays}日`;
    calendarRefreshTimer = setTimeout(() => renderCalendar(true), calendarRefreshMs);
}

async function showDetail(dateStr) {
    const requestId = ++modalRequestId;
    selectedWorkerButton = null;
    const dateObj = new Date(`${dateStr}T00:00:00`);
    const days = ['日', '月', '火', '水', '木', '金', '土'];
    const dayName = days[dateObj.getDay()];

    document.getElementById('modalTitle').textContent =
        `${dateObj.getMonth() + 1}月${dateObj.getDate()}日`;
    document.getElementById('modalDateSub').textContent =
        `${dateObj.getFullYear()}年 ${dayName}曜日`;

    setModalView('shift');

    document.getElementById('modalBody').innerHTML = `
        <div class="d-flex justify-content-center py-5">
            <div class="spinner-ring"></div>
        </div>`;

    detailModal.show();

    try {
        const response = await authenticatedFetch(`/api/shifts?date=${dateStr}`);
        if (!response.ok) throw new Error('API Error');
        const data = await response.json();
        if (requestId !== modalRequestId) return;

        const modalBody = document.getElementById('modalBody');
        modalBody.innerHTML = '';

        if (data.workers && data.workers.length > 0) {
            let separatorShown = false;
            const hasMyShift = data.has_my_shift;

            data.workers.forEach((worker) => {
                if (hasMyShift && !worker.is_me && !worker.is_overlapping && !separatorShown) {
                    const sep = document.createElement('div');
                    sep.className = 'separator';
                    sep.textContent = '時間が被っていないスタッフ';
                    modalBody.appendChild(sep);
                    separatorShown = true;
                }

                const div = document.createElement('div');
                let cardClass = 'shift-detail-card';
                let sameEndBadge = '';

                if (worker.is_me) {
                    cardClass += ' is-me';
                } else if (worker.is_same_end) {
                    cardClass += ' same-end';
                    sameEndBadge = '<span class="same-end-badge"><i class="bi bi-stars"></i> Last</span>';
                }
                div.className = cardClass;

                const rankBadge = worker.rank
                    ? `<span class="worker-rank">${escapeHtml(worker.rank)}</span>` : '';
                const ageDisplay = worker.age
                    ? `<span class="worker-age">${escapeHtml(worker.age)}歳</span>` : '';

                div.innerHTML = `
                    <div class="worker-card-header">
                        <div class="worker-identity">
                            <button type="button" class="worker-name-link">
                                <span class="worker-name">${escapeHtml(worker.name)}</span>
                            </button>
                            ${ageDisplay}
                            ${rankBadge}
                        </div>
                        <div class="worker-badges">
                            ${sameEndBadge}
                            ${worker.is_me ? '<span class="badge-me">あなた</span>' : ''}
                        </div>
                    </div>
                    <div class="worker-time">
                        <i class="bi bi-clock"></i>${escapeHtml(worker.time)}
                    </div>
                    ${worker.rest_times && worker.rest_times.length
                        ? `<div class="worker-time"><i class="bi bi-cup-hot"></i>休憩 ${escapeHtml(worker.rest_times.join(', '))}</div>` : ''}
                `;
                const workerButton = div.querySelector('.worker-name-link');
                workerButton.addEventListener('click', () => {
                    selectedWorkerButton = workerButton;
                    showStaffDetail(worker.user_id, dateObj.getFullYear(), dateObj.getMonth() + 1);
                });
                modalBody.appendChild(div);

                if (worker.is_me && hasMyShift && data.workers.length > 1) {
                    const sep = document.createElement('div');
                    sep.className = 'separator';
                    sep.textContent = '一緒に出勤するスタッフ';
                    modalBody.appendChild(sep);
                }
            });
        } else {
            modalBody.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon"><i class="bi bi-calendar-x"></i></div>
                    <p>予定されているシフトはありません</p>
                </div>`;
        }

    } catch (error) {
        if (requestId !== modalRequestId) return;
        console.error(error);
        document.getElementById('modalBody').innerHTML = `
            <div class="error-state">
                <div class="error-state-icon"><i class="bi bi-exclamation-triangle"></i></div>
                <p>データの取得に失敗しました</p>
            </div>`;
    }
}

function changeMonth(delta) {
    currentDate.setDate(1);
    currentDate.setMonth(currentDate.getMonth() + delta);
    refreshVisibleMonth();
}

function refreshVisibleMonth() {
    if (!calendarPage.hidden) {
        renderCalendar();
    } else if (!payPage.hidden) {
        updateMonthDisplay();
        updatePayMonthDisplay();
        fetchPayEstimate();
    } else {
        updateMonthDisplay();
        updateSearchMonthDisplay();
        searchStaff();
    }
}

function setActivePage(page) {
    calendarPage.hidden = page !== 'calendar';
    searchPage.hidden = page !== 'search';
    payPage.hidden = page !== 'pay';
    for (const [id, active] of [
        ['calendarNavBtn', page === 'calendar'],
        ['searchNavBtn', page === 'search'],
        ['payNavBtn', page === 'pay'],
    ]) {
        const button = document.getElementById(id);
        button.classList.toggle('is-active', active);
        if (active) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
    }
    window.scrollTo(0, 0);
}

function showCalendarPage() {
    const wasHidden = calendarPage.hidden;
    setActivePage('calendar');
    if (wasHidden) renderCalendar();
}

// スタッフ詳細表示機能
let currentStaffData = null;
let staffDetailSource = 'shift';

function formatBirthday(dateString) {
    if (!dateString) {
        return '未設定';
    }

    const parts = dateString.match(/^(\d{4})-(\d{1,2})-(\d{1,2})(?:$|T)/);
    if (!parts) {
        return dateString;
    }

    return `${parts[1]}/${parts[2].padStart(2, '0')}/${parts[3].padStart(2, '0')}`;
}

function formatDuration(totalMinutes) {
    if (!Number.isFinite(totalMinutes) || totalMinutes <= 0) {
        return '0時間00分';
    }

    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return `${hours}時間${String(minutes).padStart(2, '0')}分`;
}

async function showStaffDetail(userId, year, month, source = 'shift') {
    const requestId = ++modalRequestId;
    staffDetailSource = source;
    if (source === 'shift') shiftScrollTop = document.getElementById('modalBody').scrollTop;
    currentStaffData = null;
    document.getElementById('staffDetailName').textContent = '読み込み中...';
    document.getElementById('staffDetailCode').textContent = '—';
    document.getElementById('staffDetailBirthday').textContent = '—';

    document.getElementById('staffDetailBody').innerHTML = `
        <div class="d-flex justify-content-center py-5">
            <div class="spinner-ring"></div>
        </div>`;

    setModalView('staff');
    document.getElementById('staffDetailBody').scrollTop = 0;
    detailModal.show();

    try {
        const response = await authenticatedFetch(`/api/staff/${userId}?year=${year}&month=${month}`);
        if (!response.ok) throw new Error('API Error');

        const data = await response.json();
        if (requestId !== modalRequestId) return;
        currentStaffData = data;

        document.getElementById('staffDetailName').textContent = currentStaffData.name || '未設定';
        document.getElementById('staffDetailCode').textContent = currentStaffData.employee_code || '未設定';
        const age = currentStaffData.age;
        const ageDisplay = age === null || age === undefined || age === '' ? '年齢未設定' : `${age}歳`;
        document.getElementById('staffDetailBirthday').textContent =
            `${formatBirthday(currentStaffData.birthday)} (${ageDisplay})`;

        renderStaffSchedule();
    } catch (error) {
        if (requestId !== modalRequestId) return;
        console.error(error);
        document.getElementById('staffDetailBody').innerHTML = `
            <div class="error-state">
                <div class="error-state-icon"><i class="bi bi-exclamation-triangle"></i></div>
                <p>データの取得に失敗しました</p>
            </div>`;
    }
}

function renderStaffSchedule() {
    const body = document.getElementById('staffDetailBody');
    if (currentStaffData.schedules && currentStaffData.schedules.length > 0) {
        const totalMinutes = Number.isFinite(currentStaffData.total_work_minutes)
            ? currentStaffData.total_work_minutes
            : currentStaffData.schedules.reduce((sum, schedule) => {
                if (Number.isFinite(schedule.duration_minutes)) {
                    return sum + schedule.duration_minutes;
                }

                const timeParts = schedule.time.split(' - ');
                if (timeParts.length !== 2) {
                    return sum;
                }

                const parseMinutes = (value) => {
                    const [hours, minutes] = value.split(':').map(Number);
                    return (hours * 60) + minutes;
                };

                return sum + (parseMinutes(timeParts[1]) - parseMinutes(timeParts[0]));
            }, 0);

        const scheduleHtml = currentStaffData.schedules.map(schedule => `
            <div class="schedule-item">
                <div class="schedule-date">
                    <span class="schedule-date-main">${escapeHtml(schedule.date_display)}</span>
                    <span class="schedule-dow">(${escapeHtml(schedule.day_of_week)})</span>
                </div>
                <div class="schedule-time">
                    <span class="schedule-time-main"><i class="bi bi-clock" aria-hidden="true"></i>${escapeHtml(schedule.time)}</span>
                </div>
                ${schedule.rest_times && schedule.rest_times.length
                    ? `<div class="schedule-rest"><span>休憩</span>${schedule.rest_times.map(time => `<span>${escapeHtml(time)}</span>`).join('')}</div>` : ''}
            </div>
        `).join('');

        body.innerHTML = `
            <div class="schedule-summary">
                <div class="schedule-summary-label">今月の勤務時間合計</div>
                <div class="schedule-summary-value">${formatDuration(totalMinutes)}</div>
            </div>
            <div class="schedule-list">${scheduleHtml}</div>
        `;
    } else {
        body.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon"><i class="bi bi-calendar-x"></i></div>
                <p>この月の出勤予定はありません</p>
            </div>`;
    }
}

function backToShiftDetail() {
    if (modalView !== 'staff' || staffDetailSource !== 'shift') return;
    modalRequestId++;
    currentStaffData = null;
    setModalView('shift');
    document.getElementById('modalBody').scrollTop = shiftScrollTop;
    if (selectedWorkerButton?.isConnected) selectedWorkerButton.focus({ preventScroll: true });
}

let searchTimer;
let searchRequest = 0;

function updateSearchMonthDisplay() {
    document.getElementById('mobileSearchMonth').textContent = `${currentDate.getFullYear()}年${currentDate.getMonth() + 1}月`;
    document.getElementById('searchMonth').textContent = `${currentDate.getFullYear()}年${currentDate.getMonth() + 1}月・同じ店舗`;
}

function showSearchPage() {
    setActivePage('search');
    updateSearchMonthDisplay();
    document.getElementById('staffSearchInput').value = '';
    searchStaff();
    document.getElementById('staffSearchInput').focus();
}

async function searchStaff() {
    const requestId = ++searchRequest;
    const year = currentDate.getFullYear();
    const month = currentDate.getMonth() + 1;
    const query = document.getElementById('staffSearchInput').value.trim();
    const results = document.getElementById('searchResults');
    results.textContent = '検索中...';
    try {
        const response = await authenticatedFetch(`/api/staff?year=${year}&month=${month}&q=${encodeURIComponent(query)}`);
        if (!response.ok) throw new Error('Search failed');
        const people = await response.json();
        if (requestId !== searchRequest) return;
        results.innerHTML = '';
        if (!people.length) {
            results.textContent = '該当するスタッフはいません';
            return;
        }
        for (const person of people) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'search-person';
            button.innerHTML = `<span><strong>${escapeHtml(person.name)}</strong><small>${escapeHtml(person.employee_code || 'コード未設定')}</small></span><i class="bi bi-chevron-right"></i>`;
            button.addEventListener('click', () => showStaffDetail(person.user_id, year, month, 'search'));
            results.appendChild(button);
        }
    } catch (error) {
        if (requestId === searchRequest) results.textContent = '検索に失敗しました';
    }
}

document.getElementById('staffSearchInput').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(searchStaff, 250);
});
let payUserId = null;
let payRequest = 0;
let payTimer;

function paySettings() {
    return {
        hourly_wage: document.getElementById('hourlyWage').value,
        night_bonus_percent: document.getElementById('nightBonus').value,
    };
}

async function fetchPayEstimate(save = true) {
    const requestId = ++payRequest;
    const result = document.getElementById('payResults');
    result.textContent = '計算中...';
    const settings = paySettings();
    try {
        const response = await authenticatedFetch('/api/pay/estimate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                year: currentDate.getFullYear(), month: currentDate.getMonth() + 1,
                ...settings, hourly_wage: settings.hourly_wage || '0',
            }),
        });
        const data = await response.json();
        if (requestId !== payRequest) return;
        if (!response.ok) {
            result.textContent = data.error || '計算に失敗しました';
            return;
        }
        payUserId = data.user_id;
        if (save) {
            try { localStorage.setItem(`pay-settings-${payUserId}`, JSON.stringify(settings)); }
            catch (error) { /* Storage may be unavailable in private browsing. */ }
        }
        const hasWage = document.getElementById('hourlyWage').value !== '';
        result.innerHTML = `
            <div class="pay-total"><span>給与の概算</span><strong>${hasWage ? `${data.estimated_yen.toLocaleString('ja-JP')}円` : '時給を入力してください'}</strong></div>
            <div class="pay-row"><span>シフト</span><strong>${data.shift_count}件</strong></div>
            <div class="pay-row"><span>予定時間</span><strong>${formatDuration(data.scheduled_minutes)}</strong></div>
            <div class="pay-row"><span>休憩</span><strong>${formatDuration(data.break_minutes)}</strong></div>
            <div class="pay-row"><span>実働時間</span><strong>${formatDuration(data.worked_minutes)}</strong></div>
            <div class="pay-row"><span>深夜時間</span><strong>${formatDuration(data.night_minutes)}</strong></div>`;
    } catch (error) {
        if (requestId === payRequest) result.textContent = '計算に失敗しました';
    }
}

function updatePayMonthDisplay() {
    document.getElementById('mobilePayMonth').textContent = `${currentDate.getFullYear()}年${currentDate.getMonth() + 1}月`;
    document.getElementById('payMonth').textContent = `${currentDate.getFullYear()}年${currentDate.getMonth() + 1}月・自分のシフト`;
}

async function showPayPage() {
    setActivePage('pay');
    updatePayMonthDisplay();
    if (payUserId === null) {
        await fetchPayEstimate(false);
        if (payUserId === null) return;
        try {
            const saved = JSON.parse(localStorage.getItem(`pay-settings-${payUserId}`));
            if (saved) {
                document.getElementById('hourlyWage').value = saved.hourly_wage || '';
                document.getElementById('nightBonus').value = saved.night_bonus_percent ?? 25;
            }
        } catch (error) {
            // Ignore invalid browser settings and keep the visible defaults.
        }
    }
    fetchPayEstimate();
}

document.querySelectorAll('.pay-input').forEach(input => input.addEventListener('input', () => {
    clearTimeout(payTimer);
    payTimer = setTimeout(fetchPayEstimate, 250);
}));

renderCalendar();
