#!/usr/bin/env node

const fs = require('node:fs');

const replacementArgument = process.argv.find((argument) => argument.startsWith('--replacement='));
const replacement = replacementArgument?.slice('--replacement='.length) ?? '320x240';
const [replacementWidth, replacementHeight] = replacement.split('x');
const exitAfterReady = process.argv.includes('--exit-after-ready');
const ignoreShutdown = process.argv.includes('--ignore-shutdown');
const shutdownMarkerArgument = process.argv.find((argument) => argument.startsWith('--shutdown-marker='));
const shutdownMarker = shutdownMarkerArgument?.slice('--shutdown-marker='.length);
const commandMarkerArgument = process.argv.find((argument) => argument.startsWith('--command-marker='));
const commandMarker = commandMarkerArgument?.slice('--command-marker='.length);

process.stdout.write('RENDERER_READY\n');

if (exitAfterReady) {
  setImmediate(() => process.exit(19));
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  input += chunk;
  while (input.includes('\n')) {
    const newline = input.indexOf('\n');
    const command = input.slice(0, newline).trim();
    input = input.slice(newline + 1);
    if (command === 'RECEIVE_SURFACE') {
      process.stdout.write(`SURFACE_READY ${replacementWidth} ${replacementHeight}\n`);
    } else if (command.startsWith('FRAME')) {
      process.stdout.write('FRAME_READY\n');
    } else if (command.startsWith('SCENE_PICK ')) {
      const [, requestId, revision] = command.split(' ');
      process.stdout.write(`SCENE_SELECTION ${requestId} ${revision} c2VsZWN0ZWQ\n`);
    } else if (command.startsWith('SCENE_TRANSFORM_POINTER BEGIN ')) {
      process.stdout.write('SCENE_TRANSFORM eyJwaGFzZSI6ImJlZ2luIn0\n');
    } else if (command.startsWith('SCENE_NAVIGATE ')) {
      process.stdout.write('SCENE_CAMERA_STATE eyJwcm9qZWN0aW9uIjoicGVyc3BlY3RpdmUifQ\n');
    } else if (command === 'EMIT_MALFORMED_EVENTS') {
      process.stdout.write(
        [
          'SCENE_SELECTION invalid 3 c2VsZWN0ZWQ',
          'SCENE_SELECTION 9 3 c2VsZWN0ZWQ trailing',
          'SCENE_TRANSFORM ',
          'SCENE_TRANSFORM invalid payload',
          'SCENE_CAMERA_STATE ',
          'SCENE_CAMERA_STATE invalid payload',
          'SCENE_SELECTION 11 4 dmFsaWQ',
          'SCENE_TRANSFORM dmFsaWQ',
          'SCENE_CAMERA_STATE Y2FtZXJh',
          ''
        ].join('\n')
      );
    } else if (command.startsWith('SCENE_SNAPSHOT') && commandMarker) {
      fs.writeFileSync(commandMarker, String(Buffer.byteLength(command)));
    } else if (command === 'SHUTDOWN' && !ignoreShutdown) {
      process.stdout.write('SHUTDOWN_READY\n', (error) => {
        if (error) {
          process.exit(20);
        }
        if (shutdownMarker) {
          fs.writeFileSync(shutdownMarker, command);
        }
        process.exit(0);
      });
    } else if (command === 'QUIT' && !ignoreShutdown) {
      process.exit(0);
    }
  }
});
