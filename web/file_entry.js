'use strict';
// A file URL cannot reach the application's absolute assets and protected API.
if(window.location.protocol==='file:')window.location.replace('http://127.0.0.1:8765/nspd.html');
