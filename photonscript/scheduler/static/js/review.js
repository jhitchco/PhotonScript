// PS-24: review verdicts without a page reload, shared by the runs page
// (grid, table, lightbox) and the Targets page.
//
//   Review.set(date, file, state, {apply, revert, why})  one verdict
//   Review.setMany(date, files, state, {apply, revert})   one qa-batch call
//   Review.approve(date, files|null, {apply, revert})     approve passing subs
//   Review.undo()                                        undo the last verdict
//   Review.toast(text, isError)                          inline status line
//   Review.subState(rec) / Review.applyToRecord(rec, state) / Review.snap(rec)
//       helpers for runs-page sub records (passed_qa / reviewed / transfer)
//
// Every action is optimistic: apply() paints the new state at once, the POST
// runs in the background, and on a non-2xx or a network error revert()
// paints the old state back and a toast says why. Verdicts on one sub are
// sent one at a time in click order (per-file queue), so the server always
// ends on the last click. Pure ASCII; no framework, no CDN.
(function () {
    'use strict';
    const queues = {};      // date|file -> promise chain (per-file in-flight guard)
    const seqs = {};        // date|file -> latest request number
    const undoStack = [];   // [{date, file, prev, opts}]
    const UNDO_MAX = 200;
    let toastEl = null, toastTimer = null;

    function toast(text, isError) {
        if (!toastEl) {
            toastEl = document.createElement('div');
            toastEl.id = 'reviewToast';
            toastEl.setAttribute('role', 'status');
            toastEl.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:80;' +
                'max-width:min(420px,calc(100vw - 32px));padding:.5rem .8rem;border-radius:8px;' +
                'font-size:.85rem;box-shadow:0 4px 18px rgba(0,0,0,.45);display:none;';
            document.body.appendChild(toastEl);
        }
        toastEl.textContent = text;
        toastEl.style.background = isError ? '#7f1d1d' : '#0f172a';
        toastEl.style.color = isError ? '#fecaca' : '#e2e8f0';
        toastEl.style.border = '1px solid ' + (isError ? '#ef4444' : '#334155');
        toastEl.style.display = 'block';
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { toastEl.style.display = 'none'; },
                                isError ? 6000 : 2500);
    }

    async function post(url, body) {
        const r = await fetch(url, {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)});
        let d = null;
        try { d = await r.json(); } catch (e) { /* empty or not JSON */ }
        if (!r.ok) {
            const msg = (d && d.detail) ? d.detail : ('HTTP ' + r.status);
            throw new Error(msg);
        }
        return d || {};
    }

    function qaUrl(date, path) {
        return '/api/runs/' + encodeURIComponent(date) + '/' + path;
    }

    // ---- runs-page sub record helpers (the shape /api/runs/{date} returns)
    function subState(s) {
        return !s.passed_qa ? 'rejected' : s.reviewed ? 'accepted' : 'review';
    }
    function snap(s) {
        const keys = ['passed_qa', 'reviewed', 'transfer', 'manual_qa', 'reason',
                      'review_source', 'manual_reason'];
        const o = {};
        keys.forEach(k => { o[k] = s[k]; });
        return o;
    }
    function restore(s, o) {
        Object.keys(o).forEach(k => {
            if (o[k] === undefined) delete s[k]; else s[k] = o[k];
        });
    }
    function applyToRecord(s, state) {
        s.passed_qa = state !== 'rejected';
        s.reviewed = state !== 'review';
        s.manual_qa = state !== 'review';
        s.review_source = 'manual';
        s.reason = state === 'rejected' ? 'rejected manually' : '';
        if (state !== 'accepted') s.transfer = null;
    }
    // server fields that win over the optimistic guess once the POST lands
    const SERVER_KEYS = ['passed_qa', 'reviewed', 'manual_qa', 'reason',
                         'review_source', 'manual_reason', 'reviewed_at'];
    function merge(s, rec) {
        if (!s || !rec) return;
        SERVER_KEYS.forEach(k => {
            if (k in rec) s[k] = rec[k]; else if (k === 'manual_reason') delete s[k];
        });
    }

    function enqueue(key, fn) {
        const prev = queues[key] || Promise.resolve();
        const next = prev.catch(() => {}).then(fn);
        queues[key] = next;
        return next;
    }

    // One verdict. opts.apply(state) paints it now; opts.revert() paints the
    // previous state back if the server refuses; opts.saved(res) gets the
    // server answer ({sub, counts}). opts.prev: the state undo returns to.
    function set(date, file, state, opts) {
        opts = opts || {};
        const key = date + '|' + file;
        const seq = (seqs[key] || 0) + 1;
        seqs[key] = seq;
        if (opts.apply) opts.apply(state);
        let entry = null;
        if (!opts.noUndo && opts.prev && opts.prev !== state) {
            entry = {date: date, file: file, prev: opts.prev, opts: opts};
            undoStack.push(entry);
            if (undoStack.length > UNDO_MAX) undoStack.shift();
        }
        return enqueue(key, () => post(qaUrl(date, 'qa'),
                {file: file, state: state, why: opts.why || undefined}))
            .then(res => {
                if (seqs[key] === seq && opts.saved) opts.saved(res);
                return true;
            })
            .catch(err => {
                if (seqs[key] === seq && opts.revert) opts.revert();
                if (entry) {
                    const i = undoStack.indexOf(entry);
                    if (i >= 0) undoStack.splice(i, 1);
                }
                toast('Not saved: ' + shortName(file) + ' ' + state + ' (' +
                      err.message + '). Rolled back.', true);
                return false;
            });
    }

    // One state for many subs in one request (qa-batch).
    function setMany(date, files, state, opts) {
        opts = opts || {};
        if (!files.length) return Promise.resolve(true);
        files.forEach(f => { seqs[date + '|' + f] = (seqs[date + '|' + f] || 0) + 1; });
        const mine = files.map(f => seqs[date + '|' + f]);
        if (opts.apply) opts.apply(state);
        return post(qaUrl(date, 'qa-batch'), {files: files, state: state,
                                             why: opts.why || undefined})
            .then(res => {
                if (opts.saved) opts.saved(res);
                toast(res.updated + ' subs ' + label(state) +
                      (res.missing && res.missing.length ? ' (' + res.missing.length +
                       ' not found)' : ''), false);
                return true;
            })
            .catch(err => {
                // roll back only the subs nobody has clicked since
                const still = files.filter((f, i) => seqs[date + '|' + f] === mine[i]);
                if (opts.revert) opts.revert(still);
                toast('Not saved: ' + files.length + ' subs ' + state + ' (' +
                      err.message + '). Rolled back.', true);
                return false;
            });
    }

    // Approve passing subs awaiting review (files null = the whole night).
    function approve(date, files, opts) {
        opts = opts || {};
        if (opts.apply) opts.apply();
        return post(qaUrl(date, 'approve'), files ? {files: files} : {})
            .then(res => {
                if (opts.saved) opts.saved(res);
                toast(res.approved + ' subs approved, ' + (res.linked || 0) +
                      ' new files queued for transfer' +
                      (res.pending_review ? ' (' + res.pending_review +
                       ' on other nights still await review)' : ''), false);
                return true;
            })
            .catch(err => {
                if (opts.revert) opts.revert();
                toast('Approve failed (' + err.message + '). Rolled back.', true);
                return false;
            });
    }

    function undo() {
        const e = undoStack.pop();
        if (!e) { toast('Nothing to undo', false); return null; }
        const o = Object.assign({}, e.opts, {noUndo: true, prev: null});
        set(e.date, e.file, e.prev, o);
        toast('Undone: ' + shortName(e.file) + ' back to ' + e.prev, false);
        return e;
    }

    function label(state) {
        return state === 'accepted' ? 'accepted' : state === 'rejected' ? 'rejected'
            : 'sent back to review';
    }
    function shortName(file) {
        return String(file || '').split(/[\\/]/).pop();
    }

    window.Review = {set: set, setMany: setMany, approve: approve, undo: undo,
                     toast: toast, subState: subState, snap: snap,
                     restore: restore, applyToRecord: applyToRecord, merge: merge,
                     canUndo: () => undoStack.length > 0};
})();
