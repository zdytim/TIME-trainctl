"""Experimental anchor advancement after an exact controller response."""
from ordered_anchor_reader import append_anchor, read_anchor
from priority_chain import ControllerAnchorLinkVerifier, chain_step
from priority_protocol import verify_controller_result


class PriorityAnchorAdvancer:
    """Requires an externally authenticated, PID-pinned controller transport."""
    def __init__(self, directory, transport):
        self.directory, self.transport = directory, transport

    def __call__(self, event, result):
        epoch = self.transport.epoch
        verify_controller_result(event, result, epoch)
        last = read_anchor(self.directory)
        if last['epoch'] != epoch:
            raise ValueError('anchor installation identity changed')
        sequence = event['sequence']
        if last['sequence'] == sequence - 1:
            digest = chain_step(last['hash'], epoch, event, last['sequence'])
        elif last['sequence'] == sequence:
            # Recompute even on retry: the caller cannot substitute a different
            # request that happens to claim the same resulting user state.
            status = self.transport.status()
            if status.get('epoch') != epoch or status.get('watermark') != sequence:
                raise ValueError('controller changed before anchor retry')
            previous = self.transport.chain_lookup(status, sequence - 1)
            if chain_step(previous, epoch, event, sequence - 1) != last['hash']:
                raise ValueError('anchored operation differs from retry')
            digest = last['hash']
        else:
            raise ValueError('anchor and pending operation are not adjacent')
        result = append_anchor(self.directory, epoch, sequence, digest,
                               ControllerAnchorLinkVerifier(self.transport))
        return result == {'epoch': epoch, 'sequence': sequence, 'hash': digest}
