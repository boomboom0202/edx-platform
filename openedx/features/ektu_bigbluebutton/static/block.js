/* Shows whether the session is on, and its recordings. The block works without this. */
function EktuBigBlueButton(runtime, element, text) {
    'use strict';

    var REFRESH_MS = 30000;
    var root = element.jquery ? element[0] : element;
    var status = root.querySelector('[data-role="status"]');
    var joinButton = root.querySelector('[data-role="join"]');
    var recordings = root.querySelector('[data-role="recordings"]');
    var recordingList = root.querySelector('[data-role="recording-list"]');
    var statusUrl = runtime.handlerUrl(element, 'status');

    function showStatus(label, running) {
        status.textContent = label;
        status.classList.toggle('is-running', running);
        status.hidden = false;
    }

    function showRecordings(items) {
        recordingList.textContent = '';
        items.forEach(function(item) {
            var row = document.createElement('li');
            var date = document.createElement('span');
            var link = document.createElement('a');
            date.textContent = item.date + ' · ' + item.length;
            link.href = item.url;
            link.target = '_blank';
            link.rel = 'noopener';
            link.textContent = text.watch;
            row.appendChild(date);
            row.appendChild(link);
            recordingList.appendChild(row);
        });
        recordings.hidden = items.length === 0;
    }

    function refresh() {
        if (document.hidden) {
            return;
        }
        fetch(statusUrl, {credentials: 'same-origin'})
            .then(function(response) {
                return response.ok ? response.json() : null;
            })
            .then(function(data) {
                if (!data) {
                    return;
                }
                if (!data.available) {
                    showStatus(text.unavailable, false);
                    return;
                }
                if (data.running) {
                    showStatus(text.running + ' · ' + text.participants.replace('{count}', data.participants), true);
                    if (text.isTeam) {
                        joinButton.textContent = text.teamJoin;
                    }
                } else {
                    showStatus(text.notRunning, false);
                }
                showRecordings(data.recordings);
            })
            .catch(function() {
                // The button still works; the next refresh may succeed.
            });
    }

    refresh();
    setInterval(refresh, REFRESH_MS);
}
