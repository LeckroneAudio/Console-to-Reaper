# DiGiPanion parser

Reads a DiGiCo `.ses` file and returns all channel patching, including copy-audio destinations.

In JavaScript or TypeScript, import `parseSession` from `parser/sessionParser.ts` and call `parseSession(fileBytes, fileName)`.

From anything else, run `node digipanion-parse.mjs session.ses`. The same result is printed as JSON.
