<?php
// Serve the MQTT credentials to the page, from a file OUTSIDE the web root.
//
// The file is named after the host that asks for it, so several sites on one account can
// each have their own credentials without another script. For this project, that is
// chat.eclecity.net.txt one level above the web root — see README.md.
//
// It holds no access token: the token is typed into the page by the user, and the service
// on the PC is the only thing that checks it.

header('Content-Type: application/json');
// Never cache this: a browser holding the previous response keeps using the previous broker
// account for as long as the cache says, long after the file behind it has changed.
header('Cache-Control: no-store');

$file = '../' . $_SERVER['HTTP_HOST'] . '.txt';

if (!file_exists($file)) {
    http_response_code(404);
    echo json_encode(['error' => 'Credentials not found']);
    exit;
}

echo trim(file_get_contents($file));
