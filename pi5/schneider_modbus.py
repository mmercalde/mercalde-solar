#!/usr/bin/env python3
"""
SchneiderModbusTCP - Python Port
Direct port of mmercalde/SchneiderModbusTCP Arduino library

Handles Schneider Conext devices' specific Modbus TCP implementation:
- 32-bit values: MSW first, each word big-endian
- New TCP connection per request
- 200ms post-write delay
- 1 second response timeout
"""

import socket
import struct
import time
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class SchneiderModbusTCP:
    """
    Modbus TCP client for Schneider Conext devices.
    
    Direct port of the Arduino SchneiderModbusTCP library.
    """
    
    TIMEOUT = 1.0  # 1 second timeout
    POST_WRITE_DELAY = 0.2  # 200ms after writes
    
    def __init__(self):
        self._transaction_id = 0
    
    def _get_transaction_id(self) -> int:
        """Generate transaction ID (mimics millis() & 0xFFFF)."""
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        return self._transaction_id
    
    def _send_modbus_request(
        self,
        host: str,
        port: int,
        slave: int,
        function_code: int,
        start_reg_or_coil: int,
        quantity_or_value: int,
        send_data: Optional[bytes] = None
    ) -> Tuple[bool, Optional[bytes]]:
        """
        Send Modbus TCP request and receive response.
        
        Args:
            host: IP address of Modbus gateway
            port: TCP port (usually 503 for Schneider)
            slave: Modbus slave/unit ID
            function_code: Modbus function code (0x01, 0x03, 0x05, 0x06, 0x10)
            start_reg_or_coil: Starting register/coil address
            quantity_or_value: Quantity for reads, value for single writes
            send_data: Additional data for FC 0x10 (write multiple)
            
        Returns:
            Tuple of (success, response_data_bytes or None)
        """
        sock = None
        try:
            # Connect
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.TIMEOUT)
            sock.connect((host, port))
            
            transaction_id = self._get_transaction_id()
            protocol_id = 0  # Modbus TCP always 0
            
            # Build PDU based on function code
            if function_code in (0x01, 0x03):  # Read Coils / Read Holding Registers
                pdu = struct.pack('>BBHH', function_code, 0, start_reg_or_coil, quantity_or_value)
                pdu = pdu[1:]  # Remove the extra byte (struct quirk)
                pdu = bytes([function_code]) + struct.pack('>HH', start_reg_or_coil, quantity_or_value)
            elif function_code in (0x05, 0x06):  # Write Single Coil / Write Single Register
                pdu = bytes([function_code]) + struct.pack('>HH', start_reg_or_coil, quantity_or_value)
            elif function_code == 0x10:  # Write Multiple Registers
                byte_count = len(send_data) if send_data else 0
                pdu = bytes([function_code]) + struct.pack('>HHB', start_reg_or_coil, quantity_or_value, byte_count)
                if send_data:
                    pdu += send_data
            else:
                logger.error(f"Unsupported Modbus function code: 0x{function_code:02X}")
                return False, None
            
            # Build MBAP header
            total_length = len(pdu) + 1  # PDU + Unit ID
            mbap = struct.pack('>HHHB', transaction_id, protocol_id, total_length, slave)
            
            # Send request
            request = mbap + pdu
            sock.sendall(request)
            
            # Receive response header (minimum 9 bytes for read, 12 for write)
            if function_code in (0x01, 0x03):
                min_response = 9  # MBAP(7) + FC(1) + ByteCount(1)
            else:
                min_response = 12  # MBAP(7) + FC(1) + Addr(2) + Value(2)
            
            response_header = b''
            start_time = time.time()
            while len(response_header) < min_response:
                if time.time() - start_time > self.TIMEOUT:
                    logger.warning(f"Timeout waiting for response header (FC 0x{function_code:02X})")
                    return False, None
                try:
                    chunk = sock.recv(min_response - len(response_header))
                    if not chunk:
                        break
                    response_header += chunk
                except socket.timeout:
                    break
            
            if len(response_header) < min_response:
                logger.warning(f"Incomplete response header: got {len(response_header)}, expected {min_response}")
                return False, None
            
            # Parse MBAP header
            r_transaction_id, r_protocol_id, r_length, r_unit_id = struct.unpack('>HHHB', response_header[:7])
            
            # Verify transaction ID
            if r_transaction_id != transaction_id:
                logger.warning(f"Transaction ID mismatch: sent 0x{transaction_id:04X}, got 0x{r_transaction_id:04X}")
                return False, None
            
            r_function_code = response_header[7]
            
            # Check for exception response
            if r_function_code > 0x80:
                exception_code = response_header[8] if len(response_header) > 8 else 0
                logger.warning(f"Modbus exception from slave {slave}: FC 0x{r_function_code:02X}, code 0x{exception_code:02X}")
                return False, None
            
            # Handle response based on function code
            if function_code in (0x01, 0x03):  # Read operations
                byte_count = response_header[8]
                
                # Read remaining data bytes
                data = b''
                start_time = time.time()
                while len(data) < byte_count:
                    if time.time() - start_time > self.TIMEOUT:
                        logger.warning(f"Timeout waiting for data bytes")
                        return False, None
                    try:
                        chunk = sock.recv(byte_count - len(data))
                        if not chunk:
                            break
                        data += chunk
                    except socket.timeout:
                        break
                
                if len(data) != byte_count:
                    logger.warning(f"Incomplete data: expected {byte_count}, got {len(data)}")
                    return False, None
                
                return True, data
            else:
                # Write operations - response is echo
                logger.debug(f"Write (FC 0x{function_code:02X}) successful for slave {slave}, addr 0x{start_reg_or_coil:04X}")
                return True, None
                
        except socket.error as e:
            logger.error(f"Socket error connecting to {host}:{port}: {e}")
            return False, None
        except Exception as e:
            logger.error(f"Error in Modbus request: {e}")
            return False, None
        finally:
            if sock:
                try:
                    sock.close()
                except:
                    pass
    
    # --- Register Read Functions ---
    
    def read_holding_register_16(self, host: str, port: int, slave: int, reg: int) -> Optional[int]:
        """
        Read a single 16-bit unsigned holding register.
        
        Returns:
            16-bit unsigned value, or None on failure
        """
        success, data = self._send_modbus_request(host, port, slave, 0x03, reg, 1)
        if success and data and len(data) == 2:
            # Big-endian
            return (data[0] << 8) | data[1]
        return None
    
    def read_holding_register_32(self, host: str, port: int, slave: int, reg: int) -> Optional[int]:
        """
        Read a 32-bit unsigned value from two consecutive registers.
        
        Schneider stores 32-bit values as MSW first, each word big-endian:
        [MSB_MSW, LSB_MSW, MSB_LSW, LSB_LSW]
        
        Returns:
            32-bit unsigned value, or None on failure
        """
        success, data = self._send_modbus_request(host, port, slave, 0x03, reg, 2)
        if success and data and len(data) == 4:
            # MSW first, big-endian words
            return (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
        return None
    
    def read_holding_register_16s(self, host: str, port: int, slave: int, reg: int) -> Optional[int]:
        """
        Read a single 16-bit signed holding register.
        
        Returns:
            16-bit signed value, or None on failure
        """
        raw = self.read_holding_register_16(host, port, slave, reg)
        if raw is not None:
            # Convert to signed
            if raw >= 0x8000:
                raw -= 0x10000
            return raw
        return None
    
    def read_holding_register_32s(self, host: str, port: int, slave: int, reg: int) -> Optional[int]:
        """
        Read a 32-bit signed value from two consecutive registers.
        
        Returns:
            32-bit signed value, or None on failure
        """
        raw = self.read_holding_register_32(host, port, slave, reg)
        if raw is not None:
            # Convert to signed
            if raw >= 0x80000000:
                raw -= 0x100000000
            return raw
        return None
    
    # --- Register Write Functions ---
    
    def write_single_register_16(self, host: str, port: int, slave: int, reg: int, value: int) -> bool:
        """
        Write a single 16-bit register (FC 0x06).
        
        Returns:
            True on success, False on failure
        """
        value = value & 0xFFFF  # Ensure 16-bit
        success, _ = self._send_modbus_request(host, port, slave, 0x06, reg, value)
        if success:
            time.sleep(self.POST_WRITE_DELAY)
            logger.info(f"Wrote 16-bit value {value} to reg 0x{reg:04X} on slave {slave}")
        else:
            logger.error(f"Failed to write 16-bit value {value} to reg 0x{reg:04X} on slave {slave}")
        return success
    
    def write_single_register_32(self, host: str, port: int, slave: int, reg: int, value: int) -> bool:
        """
        Write a 32-bit value to two consecutive registers (FC 0x10).
        
        Schneider format: MSW first, each word big-endian.
        
        Returns:
            True on success, False on failure
        """
        value = value & 0xFFFFFFFF  # Ensure 32-bit
        
        msw = (value >> 16) & 0xFFFF
        lsw = value & 0xFFFF
        
        # Pack as MSW first, big-endian words
        write_data = struct.pack('>HH', msw, lsw)
        
        success, _ = self._send_modbus_request(host, port, slave, 0x10, reg, 2, write_data)
        if success:
            time.sleep(self.POST_WRITE_DELAY)
            logger.info(f"Wrote 32-bit value {value} to regs 0x{reg:04X}-0x{reg+1:04X} on slave {slave}")
        else:
            logger.error(f"Failed to write 32-bit value {value} to reg 0x{reg:04X} on slave {slave}")
        return success
    
    # --- Coil Functions ---
    
    def read_coil(self, host: str, port: int, slave: int, coil: int) -> Optional[bool]:
        """
        Read a single coil (FC 0x01).
        
        Returns:
            True if ON, False if OFF, None on failure
        """
        success, data = self._send_modbus_request(host, port, slave, 0x01, coil, 1)
        if success and data and len(data) >= 1:
            return (data[0] & 0x01) != 0
        return None
    
    def write_single_coil(self, host: str, port: int, slave: int, coil: int, value: bool) -> bool:
        """
        Write a single coil (FC 0x05).
        
        Args:
            value: True for ON (0xFF00), False for OFF (0x0000)
            
        Returns:
            True on success, False on failure
        """
        coil_value = 0xFF00 if value else 0x0000
        success, _ = self._send_modbus_request(host, port, slave, 0x05, coil, coil_value)
        if success:
            time.sleep(self.POST_WRITE_DELAY)
            logger.info(f"Wrote coil 0x{coil:04X} to {'ON' if value else 'OFF'} on slave {slave}")
        else:
            logger.error(f"Failed to write coil 0x{coil:04X} on slave {slave}")
        return success


# Convenience function for quick tests
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    
    modbus = SchneiderModbusTCP()
    
    # Example: Read battery voltage from Battery Monitor (slave 191, reg 0x0046)
    # voltage = modbus.read_holding_register_32("192.168.3.131", 503, 191, 0x0046)
    # if voltage is not None:
    #     print(f"Battery Voltage: {voltage / 1000.0:.2f} V")
    
    print("SchneiderModbusTCP Python library loaded.")
    print("Usage:")
    print("  modbus = SchneiderModbusTCP()")
    print("  voltage = modbus.read_holding_register_32('192.168.3.131', 503, 191, 0x0046)")
