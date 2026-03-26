const feed = document.getElementById('messageFeed');
const countSpan = document.getElementById('msgCount');
const refreshBtn = document.getElementById('refreshBtn');
const summaryEl = document.getElementById('threadSummary');
const analysisMetaEl = document.getElementById('analysisMeta');

let isFirstLoad = true;

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatMessageContent(value) {
    return escapeHtml(value).replace(/\n/g, '<br>');
}

function num(value, digits = 1) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
    return Number(value).toFixed(digits);
}

function hasTodoOrScheduleHint(text, analysis) {
    const source = String(text || '');
    if (!source) return false;
    const keywordPattern = /(?:todo|to-do|タスク|やること|宿題|課題|提出|締切|しめきり|期限|締め切り|予定|スケジュール|リマインド|忘れず|までに|まで|due|deadline)/i;
    const semantic = analysis?.semantic || {};
    const taskDetected = (Array.isArray(semantic.tasks) && semantic.tasks.length > 0)
        || (Array.isArray(semantic.self_tasks) && semantic.self_tasks.length > 0)
        || (Array.isArray(semantic.other_tasks) && semantic.other_tasks.length > 0);
    return keywordPattern.test(source) || taskDetected;
}

function emotionClass(label) {
    const key = String(label || '').toLowerCase();
    if (['positive', 'affectionate'].includes(key)) return 'chip-positive';
    if (['negative', 'anxious', 'urgent'].includes(key)) return 'chip-negative';
    if (key === 'mixed') return 'chip-mixed';
    return 'chip-neutral';
}

function labelChip(text, cls = '') {
    return `<span class="label-chip ${cls}">${escapeHtml(text)}</span>`;
}

function renderThreadSummary(threads, meta) {
    const provider = meta?.analysis_provider || 'fallback';
    const heavy = meta?.openai_model_heavy ? ` / heavy:${meta.openai_model_heavy}` : '';
    analysisMetaEl.textContent = `分析: ${provider}${heavy} / 未完了: ${meta?.pending_count ?? 0}`;
    if (!threads || threads.length === 0) {
        summaryEl.innerHTML = '<div class="loading">スレッドデータがありません。</div>';
        return;
    }

    let html = '';
    threads.forEach(t => {
        const participants = Array.isArray(t.participants) ? t.participants.join(' / ') : '';
        html += `
            <article class="summary-card">
                <div class="summary-head">
                    <div class="summary-participants">${escapeHtml(participants || t.thread_id)}</div>
                    <div class="summary-time">${escapeHtml(t.last_message_at || '')}</div>
                </div>
                <div class="summary-metrics">
                    <span>送信密度: <b>${num(t.messages_per_minute, 3)}</b> 件/分</span>
                    <span>話題密度: <b>${num(t.topics_per_minute, 3)}</b> 件/分</span>
                    <span>チャット/話題: <b>${num(t.messages_per_topic, 2)}</b></span>
                    <span>文字/チャット: <b>${num(t.chars_per_message, 1)}</b></span>
                    <span>継続時間: <b>${num(t.duration_minutes, 1)}</b> 分</span>
                    <span>話題数: <b>${escapeHtml(t.topic_count)}</b></span>
                </div>
            </article>
        `;
    });
    summaryEl.innerHTML = html;
}

function renderAnalysisPanel(msg) {
    const analysis = msg.analysis || {};
    const emotion = analysis.emotion || {};
    const lang = analysis.language_features || {};
    const semantic = analysis.semantic || {};
    const timing = analysis.timing || {};
    const pending = msg.analysis_status !== 'complete';
    const selfTasks = Array.isArray(semantic.self_tasks) ? semantic.self_tasks : [];
    const otherTasks = Array.isArray(semantic.other_tasks) ? semantic.other_tasks : [];
    const source = msg.analysis_source || 'fallback';
    const participantTasks = semantic.participant_tasks && typeof semantic.participant_tasks === 'object'
        ? semantic.participant_tasks
        : null;

    const expressionChips = [];
    if (lang.question_expression) expressionChips.push(labelChip('質問'));
    if (lang.interrogative_expression) expressionChips.push(labelChip('疑問'));
    if (lang.request_expression) expressionChips.push(labelChip('依頼'));
    if (lang.confirmation_expression) expressionChips.push(labelChip('確認'));
    if (lang.strong_assertion_expression) expressionChips.push(labelChip('強断定'));
    if (lang.assertive_expression) expressionChips.push(labelChip('断定'));
    if (lang.speculative_expression) expressionChips.push(labelChip('推量'));
    if (lang.impression_expression) expressionChips.push(labelChip('感想'));

    const typo = lang.typo_detected ? `あり: ${escapeHtml(lang.typo_note || '打ち間違いの可能性')}` : 'なし';
    const wordplay = lang.wordplay_detected ? `あり: ${escapeHtml(lang.wordplay_note || '言葉遊びの可能性')}` : 'なし';

    const participants = Array.isArray(msg.participants) ? msg.participants : [];
    const otherNames = participants.filter(p => {
        const k = String(p || '').trim().toLowerCase();
        return k && !['me', 'self', '自分'].includes(k);
    });
    const selfTaskList = participantTasks?.me || selfTasks || [];
    const otherCards = [];

    otherNames.forEach(name => {
        const list = Array.isArray(participantTasks?.[name]) ? participantTasks[name] : [];
        otherCards.push({ name, tasks: list });
    });

    if (otherCards.length === 0 && otherTasks.length) {
        otherCards.push({ name: '参加者', tasks: otherTasks });
    }

    otherCards.sort((a, b) => {
        const aHas = a.tasks.length > 0 ? 0 : 1;
        const bHas = b.tasks.length > 0 ? 0 : 1;
        return aHas - bHas;
    });

    const selfListHtml = selfTaskList.length
        ? selfTaskList.map(t => `<div class="task-item">・${escapeHtml(t)}</div>`).join('')
        : '<span class="muted">なし</span>';

    const otherCardsHtml = otherCards.length
        ? otherCards.map(card => `
            <article class="participant-task-card">
                <div class="participant-task-name">${escapeHtml(card.name)}のタスク</div>
                <div class="task-list">
                    ${card.tasks.length ? card.tasks.map(t => `<div class="task-item">・${escapeHtml(t)}</div>`).join('') : '<span class="muted">なし</span>'}
                </div>
            </article>
        `).join('')
        : '<span class="muted">参加者タスクなし</span>';

    return `
        <aside class="analysis-panel">
            <div class="analysis-header">
                <span class="analysis-title">分析</span>
                <div class="analysis-head-right">
                    <span class="analysis-source">${escapeHtml(source)}</span>
                    ${pending ? '<span class="analysis-pending">解析中</span>' : '<span class="analysis-done">解析済</span>'}
                </div>
            </div>
            <div class="analysis-block">
                <div class="analysis-row"><span>感情</span>${labelChip(emotion.label || 'neutral', emotionClass(emotion.label))}</div>
                <div class="analysis-row"><span>スコア</span><b>${num(emotion.score, 2)}</b></div>
                <div class="analysis-note">${escapeHtml(emotion.nuance || '解析中です')}</div>
            </div>
            <div class="analysis-block">
                <div class="analysis-row"><span>送信間隔</span><b>${num(timing.minutes_since_previous, 1)} 分</b></div>
                <div class="analysis-row"><span>話題継続</span><b>${num(timing.topic_duration_minutes, 1)} 分</b></div>
            </div>
            <div class="analysis-block">
                <div class="analysis-row"><span>話題</span><b>${escapeHtml(semantic.topic || '-')}</b></div>
                <div class="analysis-row"><span>意図</span>${labelChip(semantic.intent || 'share')}</div>
                <div class="analysis-note">${escapeHtml(semantic.content_summary || '-')}</div>
            </div>
            <div class="analysis-block">
                <div class="analysis-row"><span>打ち間違い</span><b>${typo}</b></div>
                <div class="analysis-row"><span>言葉遊び</span><b>${wordplay}</b></div>
            </div>
            <div class="analysis-block">
                <div class="analysis-row"><span>表現</span></div>
                <div class="chip-row">${expressionChips.join('') || '<span class="muted">該当なし</span>'}</div>
            </div>
            <div class="analysis-block">
                <div class="analysis-row"><span>自分のタスク</span></div>
                <div class="task-list">${selfListHtml}</div>
            </div>
            <div class="analysis-block">
                <div class="analysis-row"><span>参加者ごとのタスク</span></div>
                <div class="participant-task-scroll">
                    ${otherCardsHtml}
                </div>
            </div>
        </aside>
    `;
}

async function fetchMessages() {
    try {
        const response = await fetch('/api/messages');
        if (!response.ok) throw new Error('Network response failure');
        const payload = await response.json();
        const messages = payload.messages || [];
        const threadSummaries = payload.thread_summaries || [];
        const meta = payload.meta || {};

        countSpan.textContent = `(${messages.length})`;
        renderThreadSummary(threadSummaries, meta);

        if (messages.length === 0) {
            feed.innerHTML = '<div class="loading">No recorded messages yet.</div>';
            return;
        }

        let newHtml = '';
        messages.forEach(msg => {
            const dirClass = msg.direction === 'INCOMING' ? 'incoming-badge' : 'outgoing-badge';
            const color = msg.sentiment?.color || '#8E8E93';
            const needsAttention = hasTodoOrScheduleHint(msg.content, msg.analysis);
            const cardClass = needsAttention ? 'message-card attention-card' : 'message-card';
            const direction = escapeHtml(msg.direction);
            const sender = escapeHtml(msg.sender);
            const recipient = escapeHtml(msg.recipient);
            const timestamp = escapeHtml(msg.timestamp);
            const content = formatMessageContent(msg.content);
            const sentimentEmoji = escapeHtml(msg.sentiment?.emoji || '😐');
            const sentimentLabel = escapeHtml(msg.sentiment?.label || 'ニュートラル');

            newHtml += `
                <div class="${cardClass}">
                    <div class="message-main">
                        <div class="card-header">
                            <div class="routing">
                                <span class="dir-badge ${dirClass}">${direction}</span>
                                <span>${sender} → ${recipient}</span>
                            </div>
                            <div class="timestamp">${timestamp}</div>
                        </div>
                        <div class="card-body">${content}</div>
                        <div class="card-footer">
                            <div class="sentiment-badge" style="border-color: ${color}40; color: ${color}">
                                <span>${sentimentEmoji}</span>
                                <span>${sentimentLabel}</span>
                            </div>
                        </div>
                    </div>
                    ${renderAnalysisPanel(msg)}
                </div>
            `;
        });

        feed.innerHTML = newHtml;
        isFirstLoad = false;
    } catch (error) {
        console.error('Error fetching API:', error);
        if (isFirstLoad) {
            feed.innerHTML = `<div class="loading" style="color: #ef4444;">Failed to load messages. Is Python API server running?</div>`;
            summaryEl.innerHTML = '<div class="loading" style="color: #ef4444;">集計の取得に失敗しました。</div>';
        }
    }
}

fetchMessages();
setInterval(fetchMessages, 3000);

refreshBtn.addEventListener('click', () => {
    refreshBtn.style.opacity = '0.5';
    fetchMessages().then(() => {
        setTimeout(() => {
            refreshBtn.style.opacity = '1';
        }, 200);
    });
});
