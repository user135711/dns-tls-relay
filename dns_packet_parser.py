#!/usr/bin/env python3

import struct
import traceback

from log_methods import record_parse_error

TCP = 6
UDP = 17

A_RECORD = 1
CNAME = 5
SOA = 6
OPT = 41

DEFAULT_TTL = 3600
MINIMUM_TTL = 300
MAX_A_RECORD_COUNT = 3

DNS_QUERY = 0
DNS_RESPONSE = 128


class PacketManipulation:
    def __init__(self, data, protocol):
        if (protocol == UDP):
            self.data = data
        elif (protocol == TCP):
            self.data = data[2:]

        self.dns_id = 0
        self.qtype = 0
        self.qclass = 0
        self.cache_ttl = 0

        self.dns_opt = False
        self.dns_response = False
        self.dns_pointer = b'\xc0\x0c'

        self.cache_header = b''
        self.send_data = b''

        self.offset = 0
        self.cname_count = 0
        self.a_record_count = 0
        self.standard_records = []
        self.authority_records =[]
        self.additional_records = []

    def parse(self):
        try:
            self.header()
            if (self.packet_type in {DNS_QUERY, DNS_RESPONSE}):
                self.question_record_handler()
                self.get_qname()
                if (self.packet_type == DNS_RESPONSE):
                    self.resource_record_handler()

        except Exception:
            traceback.print_exc()

    def get_dns_id(self):
        dns_id = struct.unpack('!H', self.data[:2])[0]

        return dns_id

    def header(self):
        self.dns_header = self.data[:12]

        self.dns_id = struct.unpack('!H', self.data[:2])[0]

        self.packet_type = self.dns_header[2] & 1 << 7
        if (self.dns_header[2] & 1 << 7): # Response
            self.dns_response = True
        else:
            self.dns_query = True

        content_info = struct.unpack('!4H', self.dns_header[4:12])
        self.question_count = content_info[0]
        self.standard_count = content_info[1] #answer count (name standard for iteration purposes in parsing)
        self.authority_count = content_info[2]
        self.additional_count = content_info[3]

    def question_record_handler(self):
        dns_payload = self.data[12:]

        query_info = dns_payload.split(b'\x00', 1)
        record_type_info = struct.unpack('!2H', query_info[1][0:4])
        self.query_name = query_info[0]
        self.qtype = record_type_info[0]
        self.qclass = record_type_info[1]

        name_length = len(self.query_name)
        question_length = name_length + 1 + 4 # name, pad, data

        self.question_record = dns_payload[:question_length]
        self.resource_record = dns_payload[question_length:]

    def get_record_type(self, data):
        #checking if record starts with a pointer/is a pointer
        if (data.startswith(b'\xc0')):
            record_name = data[:2]
        else:
            record_name = data.split(b'\x00', 1)[0]

        nlen = len(record_name)
        #if record contains a pointer, no action taken, if not 1 will be added to the length to adjust for the pad at the end of the name
        if (b'\xc0' not in record_name):
            nlen += 1

        record_type = struct.unpack('!H', data[nlen:nlen+2])[0]
        if (record_type == A_RECORD):
            record_length = 10 + 4 + nlen

        elif (record_type in {CNAME, SOA}):
            data_length = struct.unpack('!H', data[nlen+8:nlen+10])[0]
            record_length = 10 + data_length + nlen

        # to catch errors with record type parsing and allow for troubleshooting (TEMPORARY)
        else:
            log_info = {'rtype': record_type, 'nlen': nlen, 'data': data}
            record_parse_error(log_info)
            record_length = -1

        record_ttl = struct.unpack('!L', data[nlen+4:nlen+8])[0]

        return record_type, record_length, record_ttl, nlen

    # grabbing the records contained in the packet and appending them to their designated lists to be inspected by other methods.
    # count of records is being grabbed/used from the header information
    def resource_record_handler(self):
        # parsing standard and authority records
        for record_type in ['standard', 'authority']:
            record_count = getattr(self, f'{record_type}_count')
            records_list = getattr(self, f'{record_type}_records')
            for _ in range(record_count):
                data = self.resource_record[self.offset:]
                record_type, record_length, record_ttl, nlen = self.get_record_type(data)

                resource_record = data[:record_length]
#                print((record_type, record_ttl, resource_record))
                records_list.append((record_type, record_ttl, nlen, resource_record))

                self.offset += record_length

        # parsing additional records
        for _ in range(self.additional_count):
            data = self.resource_record[self.offset:]
            additional_type = struct.unpack('!H', data[1:3])
            if additional_type == OPT:
                self.dns_opt = True

            self.additional_records.append(data)

    def rewrite(self, dns_id=None, response_ttl=DEFAULT_TTL):
        resource_record = b''
        for record_type in ['standard', 'authority']:
            all_records = getattr(self, f'{record_type}_records')
            for record_info in all_records:
                record_type = record_info[0]
                if (record_type != A_RECORD or self.a_record_count < MAX_A_RECORD_COUNT):
                    record = self.ttl_rewrite(record_info, response_ttl)

                    resource_record += record

        # rewriting answer record count if a record count is over max due to limiting record total
        if (self.a_record_count == MAX_A_RECORD_COUNT):
            answer_count = struct.pack('!H', MAX_A_RECORD_COUNT)
            self.dns_header = self.dns_header[:6] + answer_count + self.dns_header[8:]

        # setting add record count to 0 and assigning variable for data to cache prior to appending additional records
        self.data_to_cache = self.dns_header[:10] + b'\x00'*2 + self.question_record + resource_record

        # additional records will remain intact until otherwise needed
        for record in (self.additional_records):
            resource_record += record

        # Replacing tcp dns id with original client dns id if converting back from tcp/tls.
        if (dns_id):
            self.dns_header = struct.pack('!H', dns_id) + self.dns_header[2:]

        self.send_data += self.dns_header + self.question_record + resource_record

    def ttl_rewrite(self, record_info, response_ttl):
        record_type, record_ttl, nlen, record = record_info
        # incrementing a record counter to limit amount of records in response/held in cache to configured ammount
        if (record_type == A_RECORD):
            self.a_record_count += 1

        if (record_ttl < MINIMUM_TTL):
            new_record_ttl = MINIMUM_TTL
        # rewriting ttl to the remaining amount that was calculated from cached packet or to the maximum defined TTL
        elif (record_ttl > DEFAULT_TTL):
            new_record_ttl = DEFAULT_TTL
        # anything in between the min and max TTL will be retained
        else:
            new_record_ttl = record_ttl
        self.cache_ttl = new_record_ttl

        record_front = record[:nlen+4]
        new_record_ttl = struct.pack('!L', new_record_ttl)
        record_back = record[nlen+8:]

        # returning rewrittin resource record
        return record_front + new_record_ttl + record_back

    def get_qname(self):
        b = len(self.query_name)
        qname = struct.unpack(f'!{b}B', self.query_name)

        # coverting query name from bytes to string
        length = qname[0]
        qname_raw = ''
        for byte in qname[1:]:
            if (length != 0):
                qname_raw += chr(byte)
                length -= 1
                continue

            length = byte
            qname_raw += '.'

        self.request = qname_raw.lower() # www.micro.com or micro.com || sd.micro.com
        if ('.' in qname):
            req = qname.split('.')
            self.request2 = f'{req[-2]}.{req[-1]}' # micro.com or co.uk
            self.request_tld = f'.{req[-1]}' # .com

    def revert_response(self):
        dns_payload = self.data[12:]

        # creating empty dns header, with standard query flag and recursion flag. will be rewritten with proper dns id
        # at another point in the process
        dns_header = struct.pack('H4B3H', 0,1,0,0,1,0,0,0)

        dns_query = dns_payload.split(b'\x00',1)
        query_name = dns_query[0]

        self.data = dns_header + query_name + b'\x00' + dns_query[1][0:4]

    def udp_to_tls(self, dns_id):
        payload_length = struct.pack('!H', len(self.data))
        tcp_dns_id = struct.pack('!H', dns_id)

        tcp_dns_payload = payload_length + tcp_dns_id + self.data[2:]

        return tcp_dns_payload
