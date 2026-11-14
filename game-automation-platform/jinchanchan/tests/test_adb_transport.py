import asyncio
import unittest

from agent.sdk.sdk import Sdk


class FakeBinaryTransport:
    def __init__(self, ok=True, output=b"Physical density: 240\nOverride density: 280\n"):
        self.ok = ok
        self.output = output

    def shell(self, command):
        self.asserted_command = command
        return self.ok, self.output


class AdbTransportTests(unittest.TestCase):
    def test_screen_density_uses_binary_transport(self):
        transport = FakeBinaryTransport()
        sdk = Sdk(transport=transport)

        density = asyncio.run(sdk.get_screen_density())

        self.assertEqual("wm density", transport.asserted_command)
        self.assertEqual(280, density)

    def test_screen_density_returns_zero_when_transport_fails(self):
        sdk = Sdk(transport=FakeBinaryTransport(ok=False))

        self.assertEqual(0, asyncio.run(sdk.get_screen_density()))


if __name__ == "__main__":
    unittest.main()
