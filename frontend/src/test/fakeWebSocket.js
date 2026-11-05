import { vi } from 'vitest';

/** jsdom has no WebSocket. Tests drive the "server" side through this stub. */
export class FakeWebSocket {
  static instances = [];

  static get last() {
    return FakeWebSocket.instances.at(-1);
  }

  constructor(url) {
    this.url = url;
    this.sent = [];
    this.closed = false;
    FakeWebSocket.instances.push(this);
  }

  send(data) {
    this.sent.push(JSON.parse(data));
  }

  close(code = 1000) {
    this.closed = true;
    this.onclose?.({ code });
  }

  // --- server side ---
  serverOpen() {
    this.onopen?.({});
  }

  serverSend(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  serverClose(code) {
    this.closed = true;
    this.onclose?.({ code });
  }
}

export function installFakeWebSocket() {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  return FakeWebSocket;
}
