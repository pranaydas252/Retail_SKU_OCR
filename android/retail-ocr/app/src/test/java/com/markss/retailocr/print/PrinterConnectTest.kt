package com.markss.retailocr.print

import com.zebra.sdk.comm.Connection
import com.zebra.sdk.comm.ConnectionException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The retry that hides Android's first-connect RFCOMM failure.
 *
 * Every case here is one that was seen or reasoned about on the TC22 against a
 * real ZQ320, not a hypothetical: the transient failure that succeeds on the
 * second try, and the genuinely-off printer that must still fail rather than
 * loop.
 */
class PrinterConnectTest {

    /** Fails its first [failures] open() calls, then succeeds. */
    private class FakeConnection(private var failures: Int) : Connection {
        var opened = false
        var closed = false

        override fun open() {
            if (failures > 0) {
                failures--
                throw ConnectionException("read failed, socket might closed or timeout")
            }
            opened = true
        }

        override fun close() {
            closed = true
        }

        // The rest of the Connection interface, which the retry never touches.
        // Signatures taken from javap on ZSDK_ANDROID_API.jar rather than
        // guessed - the interface is wider than it looks and a wrong signature
        // here fails as "class is not abstract", which reads like a Kotlin
        // problem rather than a stale assumption about the SDK.
        override fun write(data: ByteArray?) = Unit
        override fun write(data: ByteArray?, offset: Int, length: Int) = Unit
        override fun write(stream: java.io.InputStream?) = Unit
        override fun read(): ByteArray = ByteArray(0)
        override fun read(stream: java.io.OutputStream?) = Unit
        override fun readChar(): Int = -1
        override fun isConnected(): Boolean = opened
        override fun bytesAvailable(): Int = 0
        override fun getSimpleConnectionName(): String = "fake"
        override fun waitForData(millis: Int) = Unit
        override fun getMaxTimeoutForRead(): Int = 0
        override fun getTimeToWaitForMoreData(): Int = 0
        override fun setMaxTimeoutForRead(value: Int) = Unit
        override fun setTimeToWaitForMoreData(value: Int) = Unit
        override fun sendAndWaitForResponse(
            data: ByteArray?, a: Int, b: Int, terminator: String?,
        ): ByteArray = ByteArray(0)
        override fun sendAndWaitForResponse(
            out: java.io.OutputStream?, input: java.io.InputStream?,
            a: Int, b: Int, terminator: String?,
        ) = Unit
        override fun sendAndWaitForValidResponse(
            data: ByteArray?, a: Int, b: Int,
            validator: com.zebra.sdk.comm.ResponseValidator?,
        ): ByteArray = ByteArray(0)
        override fun sendAndWaitForValidResponse(
            out: java.io.OutputStream?, input: java.io.InputStream?,
            a: Int, b: Int, validator: com.zebra.sdk.comm.ResponseValidator?,
        ) = Unit
        override fun getConnectionReestablisher(
            delay: Long,
        ): com.zebra.sdk.comm.ConnectionReestablisher =
            throw UnsupportedOperationException("not used by the retry")
    }

    private class Factory(private val failuresPerConnection: List<Int>) : (String) -> Connection {
        val built = mutableListOf<FakeConnection>()
        override fun invoke(mac: String): Connection {
            val failures = failuresPerConnection.getOrElse(built.size) { 0 }
            return FakeConnection(failures).also { built += it }
        }
    }

    private val slept = mutableListOf<Long>()
    private val logged = mutableListOf<String>()

    private fun open(factory: Factory, attempts: Int = PrinterConnect.ATTEMPTS) =
        PrinterConnect.open(
            mac = "CC:B5:4C:C8:13:74",
            attempts = attempts,
            delayMs = 500L,
            sleep = { slept += it },
            log = { logged += it },
            factory = factory,
        )

    @Test
    fun `a clean connect does not retry or sleep`() {
        val factory = Factory(listOf(0))

        val connection = open(factory)

        assertEquals(1, factory.built.size)
        assertSame(factory.built[0], connection)
        assertTrue(slept.isEmpty())
    }

    @Test
    fun `the observed transient failure is hidden from the operator`() {
        // The exact case from the TC22: first connect throws, second succeeds,
        // printer never moved.
        val factory = Factory(listOf(1, 0))

        val connection = open(factory)

        assertEquals(2, factory.built.size)
        assertSame(factory.built[1], connection)
        assertEquals(listOf(500L), slept)
    }

    @Test
    fun `the spent socket is closed before retrying`() {
        // Leaving it open makes the retry contend with a half-open connection
        // to the same device, which is how a retry turns into a second failure.
        val factory = Factory(listOf(1, 0))

        open(factory)

        assertTrue("first connection was not closed", factory.built[0].closed)
    }

    @Test
    fun `each attempt gets a fresh connection`() {
        // A connection whose open() threw cannot be reopened - the underlying
        // socket is spent - so reusing one would guarantee the retry fails.
        val factory = Factory(listOf(1, 0))

        open(factory)

        assertEquals(2, factory.built.size)
        assertTrue(factory.built[0] !== factory.built[1])
    }

    @Test
    fun `a printer that is genuinely off still fails`() {
        // The retry must not turn "switched off" into an unbounded wait.
        val factory = Factory(listOf(1, 1))

        assertThrows(ConnectionException::class.java) { open(factory) }

        assertEquals(PrinterConnect.ATTEMPTS, factory.built.size)
    }

    @Test
    fun `the last failure is what propagates`() {
        val factory = Factory(listOf(1, 1))

        val error = assertThrows(ConnectionException::class.java) { open(factory) }

        // Not a synthesized message: the caller logs this, and a made-up one
        // would hide what the stack actually said.
        assertTrue(error.message!!.contains("socket might closed"))
    }

    @Test
    fun `a single attempt disables the retry entirely`() {
        val factory = Factory(listOf(1))

        assertThrows(ConnectionException::class.java) { open(factory, attempts = 1) }

        assertEquals(1, factory.built.size)
        assertTrue("must not sleep when there is no retry", slept.isEmpty())
    }

    @Test
    fun `retrying is announced so the flake is visible in logs`() {
        // Silently swallowing it would hide a printer that is failing every
        // first connect because something is actually wrong with it.
        open(Factory(listOf(1, 0)))

        assertEquals(2, logged.size)
        assertTrue(logged[0].contains("Connect attempt 1"))
        assertTrue(logged[1].contains("attempt 2"))
    }
}
