/* The review player of EKTU proctoring: plays the screen and the webcam
   recordings side by side and keeps them in step. */
(function() {
    'use strict';

    var data = JSON.parse(document.getElementById('ektu-proctoring-player').textContent);
    var main = document.getElementById('pr-main');
    var side = document.getElementById('pr-side');
    var partsBox = document.getElementById('pr-parts');
    // A recorder that was restarted leaves several files of one kind: the big
    // picture plays them as parts, the small one follows by the clock.
    var lead = data.screen.length ? 'screen' : 'webcam';
    var index = 0;
    if (!main || !data[lead].length) { return; }
    // The sound is in the webcam recording, whichever picture is the big one.
    main.muted = lead === 'screen';

    function parts() { return data[lead]; }
    function others() { return data[lead === 'screen' ? 'webcam' : 'screen']; }
    function clock() { return parts()[index].start + main.currentTime * 1000; }

    function partAt(list, moment) {
        var found = -1;
        list.forEach(function(recording, number) {
            if (recording.start <= moment + 500) { found = number; }
        });
        return found;
    }

    function follow() {
        var recording = others()[partAt(others(), clock())];
        if (!recording) {
            side.hidden = true;
            side.pause();
            return;
        }
        if (side.dataset.url !== recording.url) {
            side.dataset.url = recording.url;
            side.src = recording.url;
        }
        side.hidden = false;
        var wanted = Math.max((clock() - recording.start) / 1000, 0);
        if (Math.abs(side.currentTime - wanted) > 0.7) { side.currentTime = wanted; }
        if (main.paused !== side.paused) {
            if (main.paused) { side.pause(); } else { side.play().catch(function() {}); }
        }
        side.playbackRate = main.playbackRate;
    }

    function drawParts() {
        partsBox.textContent = '';
        document.getElementById('pr-parts-label').hidden = parts().length < 2;
        if (parts().length < 2) { return; }
        parts().forEach(function(recording, number) {
            var button = document.createElement('button');
            button.type = 'button';
            button.textContent = number + 1;
            button.className = number === index ? 'is-active' : '';
            button.addEventListener('click', function() { load(number); });
            partsBox.appendChild(button);
        });
    }

    function load(number, at, play) {
        index = number;
        side.dataset.url = '';
        main.src = parts()[number].url;
        drawParts();
        main.addEventListener('loadedmetadata', function once() {
            main.removeEventListener('loadedmetadata', once);
            if (at) { main.currentTime = at; }
            follow();
            if (play) { main.play().catch(function() {}); }
        });
    }

    function open(moment, play) {
        var number = Math.max(partAt(parts(), moment), 0);
        var at = Math.max((moment - parts()[number].start) / 1000, 0);
        if (number === index && main.readyState) {
            main.currentTime = at;
            if (play) { main.play().catch(function() {}); }
        } else {
            load(number, at, play);
        }
    }

    ['play', 'pause', 'seeked', 'ratechange', 'timeupdate'].forEach(function(name) {
        main.addEventListener(name, follow);
    });
    main.addEventListener('ended', function() {
        if (index + 1 < parts().length) { load(index + 1, 0, true); }
    });

    document.querySelectorAll('[data-speed]').forEach(function(button) {
        button.addEventListener('click', function() {
            main.playbackRate = Number(button.dataset.speed);
            document.querySelectorAll('[data-speed]').forEach(function(other) {
                other.classList.toggle('is-active', other === button);
            });
        });
    });

    var swap = document.getElementById('pr-swap');
    swap.hidden = !(data.screen.length && data.webcam.length);
    swap.addEventListener('click', function() {
        var moment = clock();
        var playing = !main.paused;
        lead = lead === 'screen' ? 'webcam' : 'screen';
        main.muted = lead === 'screen';
        side.muted = lead !== 'screen' || !sound.classList.contains('is-active');
        index = -1;
        open(moment, playing);
    });

    var sound = document.getElementById('pr-sound');
    sound.hidden = !(data.screen.length && data.webcam.length);
    sound.addEventListener('click', function() {
        var on = sound.classList.toggle('is-active');
        if (lead === 'screen') { side.muted = !on; } else { main.muted = !on; }
    });

    // An event opens the part it happened in, a few seconds before the moment.
    document.querySelectorAll('[data-offset]').forEach(function(link) {
        link.addEventListener('click', function(event) {
            event.preventDefault();
            open(Math.max(Number(link.dataset.offset) - 3000, parts()[0].start), true);
            main.scrollIntoView({behavior: 'smooth', block: 'center'});
        });
    });

    load(0);
}());
