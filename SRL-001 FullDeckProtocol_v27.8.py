import math
import string 
import serial 
import glob 
from opentrons import protocol_api
from opentrons import types
import collections
import bisect

metadata = {'apiLevel': '2.9'}

def run(protocol: protocol_api.ProtocolContext):
    offset = { 'x': 0, 'y': 0 }
    tuberack = protocol.load_labware('cosmas_and_damian_drybath_tuberack', 10)
    tiprack = protocol.load_labware('cosmas_and_damian_biotix_96_200ul_tiprack', 11)
    pipette = protocol.load_instrument('p300_single_gen2', mount='left', tip_racks=[tiprack])
    carriage = protocol.load_labware('cosmas_and_damian_nextgen_cartridge_carriage_v1_30', 1)

    for port in glob.glob('/dev/ttyACM?'):
        try:
            ser = serial.Serial(port=port, baudrate=115200, timeout=0.1)
        except:
            print('Exception in testing port {}'.format(port))
    
    if (not ser.is_open):
        protocol.pause('Unable to open serial port - click Resume to continue')
        return
    else:
        ser.write(b'C')
        offset_data = str(ser.readline()).split(':')
        offset['x'] = float(offset_data[0][2:])
        offset['y'] = float(offset_data[1][:-5])
        # protocol.pause('{0}, x: {1}, y: {2}'.format(offset_data, offset['x'], offset['y']))

    def pick_up_and_calibrate_tip():
        z_cal = 21.5
        
        if (pipette.has_tip):
            pipette.drop_tip()
        pipette.pick_up_tip()

        pipette.move_to(types.Location(types.Point(x=158, y=252.5, z=z_cal), carriage), speed=None)

        x_pos = 164
        y_pos = 256
        pipette.move_to(types.Location(types.Point(x=x_pos, y=y_pos, z=z_cal), carriage), force_direct=True, speed=20)
        limit_reached = False
        shift = 0.1
        ser.write(b'X')
        while (not limit_reached):
            pipette.move_to(types.Location(types.Point(x=x_pos + shift, y=y_pos, z=z_cal), carriage), force_direct=True, speed=5)
            shift += 0.1
            if (shift > 5):
                protocol.pause('Unable to calibrate X axis, xOffset=' + str(x_pos + shift) + ' - click Resume to end')
                break
            limit_reached = ser.read() == b'X'
        xOffset = round(x_pos + shift - 165 + offset['x'], 1)

        x_pos = 161
        y_pos = 246
        pipette.move_to(types.Location(types.Point(x=x_pos, y=y_pos, z=z_cal), carriage), force_direct=True, speed=20)
        limit_reached = False
        shift = 0.1
        ser.write(b'Y')
        while (not limit_reached):
            pipette.move_to(types.Location(types.Point(x=x_pos, y=y_pos - shift, z=z_cal), carriage), force_direct=True, speed=5)
            shift += 0.1
            if (shift > 5):
                protocol.pause('Unable to calibrate Y axis, yOffset=' + str(y_pos - shift) + ' - click Resume to continue')
                break
            limit_reached = ser.read() == b'Y'
        yOffset = round(y_pos - shift - 244.5 + offset['y'], 1)

        pipette.move_to(types.Location(types.Point(x=158, y=252.5, z=z_cal), carriage), force_direct=True, speed=20)
        pipette.move_to(types.Location(types.Point(x=158, y=252.5, z=z_cal + 20), carriage), force_direct=True, speed=20)
        
        return { 'x': xOffset, 'y': yOffset }


    disposal_volume = 20
    maximum_volume_per_aspiration = 180
    pipette_tip_capacity = 210
    tube_rim_height_2mL = 107.55
    aspirating_z_height_tweak = 1.75
    max_aspirate_depth = 50+aspirating_z_height_tweak
    
    

    def source_height(source_volume):
        #create a table that defines the liquid height in mm from the bottom of the test tube, for specific volumes of water
        lookup_table_2mL = collections.OrderedDict()
        lookup_table_2mL = {
            50:3.80,
            100:4.15,
            150:5.65,
            200:6.57,
            300:7.71,
            400:9.90,
            500:11.10,
            750:15.01,
            1000:19.12,
            1500:26.33,
            2000:33.60,
            2250:37.06
        }
        # define the bottom of the tube in relation to the deck of the robot 
        tube_bottom_height = 68.48
        
        # create a list of the keys (volumes) from the lookup table
        lookup_table_keys_list = list(lookup_table_2mL) 

        # based on the source volume, return the dictionary location of next lowest key
        closet_volume = bisect.bisect_left(list(lookup_table_2mL.keys()), source_volume)
        
        # determine how many mm of liquid height does the source volume extend past the next lowest key
        linear_ratio_between_volumes = (source_volume - lookup_table_keys_list[closet_volume]) / (lookup_table_keys_list[closet_volume+1]-lookup_table_keys_list[closet_volume])
        tube_liquid_height_correction = (lookup_table_2mL[lookup_table_keys_list[closet_volume+1]]-lookup_table_2mL[lookup_table_keys_list[closet_volume]]) * linear_ratio_between_volumes

        # define the tube liquid height
        tube_liquid_height = lookup_table_2mL[lookup_table_keys_list[closet_volume]] + tube_liquid_height_correction

        source_height = tube_bottom_height + tube_liquid_height
        return source_height


    def dispense_reagent(sources, wells, well_volume, source_volume, adjust, cartridges_per_deck):
        # fill all wells

        #define an array wells_to_fill that truncates the longer array that fills 20 cartridges by the amount of cartridges that are to be filled
        wells_to_fill = []
        if sources == [tube_locations['magnetic_beads']] or sources == [tube_locations['wash_buffer']] or sources == [tube_locations['tracer']] or sources == [tube_locations['tmb']]:
            for i in range(cartridges_per_deck*3):
                wells_to_fill.append(wells[i])
        else:
            for i in range(cartridges_per_deck):
                wells_to_fill.append(wells[i])

        for source in sources:
            source_well = tuberack[source]
            destination_wells = [carriage[well] for well in wells_to_fill]
            wells_per_run = math.floor((150 if source in ['C2'] else maximum_volume_per_aspiration) / well_volume)
            runs = math.ceil(len(destination_wells) / wells_per_run)
            print('runs for:', source, runs)

            #count how many wells have been filled, so the robot knows when to jump over a well
            jump_count = 0
            jump_frequency = 40
            jump_height = 40
            #set jump height to 40 to activate

            for run in range(runs):
                start_well = run * wells_per_run
                wells_to_fill = destination_wells[start_well:start_well + wells_per_run]
                aspirate_volume = len(wells_to_fill) * well_volume + disposal_volume
                aspirate_depth = tube_rim_height_2mL - source_height(source_volume)
                aspirate_end = tube_rim_height_2mL - source_height(source_volume - aspirate_volume)

                #adjust the aspirate cover based on how close you are getting to the bottom of the tube
                if aspirate_end < 30-aspirating_z_height_tweak:
                    aspirate_cover = 4.5+aspirating_z_height_tweak
                else:
                    aspirate_cover = 3+aspirating_z_height_tweak

                protocol.comment(aspirate_end)
                protocol.comment(aspirate_cover)

                print('# of wells, aspirate_volume', len(wells_to_fill), aspirate_volume)
                
                if (aspirate_end + aspirate_cover) > max_aspirate_depth:
                    protocol.comment('Aspirating too deep, pipette tip will be damaged, run aborted')
                    break
                if aspirate_depth < aspirate_cover:
                    print('WARNING: aspirating too deep', aspirate_depth)
                if aspirate_volume > pipette_tip_capacity:
                    print('WARNING: aspirating too much volume', aspirate_volume)
            
               #pipette.move_to(source_well.top(7),3)
                pipette.aspirate(aspirate_volume, source_well.top(-(aspirate_end + aspirate_cover)))
                if sources == [tube_locations['ch_b']]:
                    pipette.touch_tip(source_well,v_offset = -3, speed=50)
                pipette.dispense(.25, source_well.top(), rate = 1.6)

                count = 0
                dispensed_volume = 0
                first_dispense_correction_tmb = 3
                first_dispense_correction_hrp = 3
                first_dispense_correction_cm = 3
                
                well_z_depth = -5.25

                ## defines dispensing pattern everytime the pippette has filled with reagents
                for well in wells_to_fill:
                    protocol.comment(sources)
                    if count == 0 and sources == [tube_locations['tmb']]:
                        if jump_count % jump_frequency == 0 or sources ==[tube_locations['ch_b']]:
                            pipette.move_to(well.top(jump_height))
                        pipette.dispense(well_volume + first_dispense_correction_tmb, well.top(well_z_depth).move(types.Point(adjust['x'], adjust['y'], 0.0)), rate=1)
                        protocol.delay(seconds = .25)
                        count += 1
                        jump_count += 1
                        dispensed_volume += well_volume + first_dispense_correction_tmb

                    elif count == 0 and sources == [tube_locations['tracer']]:
                        if jump_count % jump_frequency == 0 or sources ==[tube_locations['ch_b']]:
                            pipette.move_to(well.top(jump_height))
                        pipette.dispense(well_volume + first_dispense_correction_hrp, well.top(well_z_depth).move(types.Point(adjust['x'], adjust['y'], 0.0)), rate=1)
                        protocol.delay(seconds = .25)
                        count += 1
                        jump_count += 1
                        dispensed_volume += well_volume + first_dispense_correction_hrp
                    
                    elif count == 0 and sources == [tube_locations['ch_b']]:
                        if jump_count % jump_frequency == 0 or sources ==[tube_locations['ch_b']]:
                            pipette.move_to(well.top(jump_height))
                        pipette.dispense(well_volume + first_dispense_correction_cm, well.top(well_z_depth).move(types.Point(adjust['x'], adjust['y'], 0.0)), rate=1)
                        protocol.delay(seconds = .25)
                        count += 1
                        jump_count += 1
                        dispensed_volume += well_volume + first_dispense_correction_cm

                    else:
                        if jump_count % jump_frequency == 0 or sources ==[tube_locations['ch_b']]:
                            pipette.move_to(well.top(jump_height))
                        pipette.dispense(well_volume, well.top(well_z_depth).move(types.Point(adjust['x'], adjust['y'], 0.0)), rate=1)
                        protocol.delay(seconds = 0.25)
                        count += 1
                        jump_count += 1
                        dispensed_volume += well_volume
                 
                source_volume -= dispensed_volume

                if (run < runs - 1 or runs == 1):
                    ##if not TMB don't dispense the 40 else dispense a smaller amount
                    pipette.dispense(40, source_well.top())
        pipette.drop_tip()
    
    params = {
        'ch_a': { 'cols': ['1', '6', '11'], 'skip': 3, 'offset': 2 },
        'ch_b': { 'cols': ['1', '6', '11'], 'skip': 3, 'offset': 1 },
        'ch_c': { 'cols': ['1', '6', '11'], 'skip': 3, 'offset': 0 },
        'magnetic_beads': { 'cols': ['2', '7', '12'], 'skip': 1, 'offset': 0 },
        'tracer': { 'cols': ['3', '8', '13'], 'skip': 1, 'offset': 0 },
        'wash_buffer': { 'cols': ['4', '9', '14'], 'skip': 1, 'offset': 0 },
        'tmb': { 'cols': ['5', '10', '15'], 'skip': 1, 'offset': 0 }
    }
    reagents = list(params)
    rows = [list('UTSRQPONMLKJIHGFEDCBA'), list('ABCDEFGHIJKLMNOPQR'), list('RQPUTSONMLKJIHGFEDCBA')]

    #combines params and rows to create an array in format A1, B1...
    destinations = dict(
        zip(
            reagents,
            (
                [ row + col for num, col in enumerate(params[reagent]['cols']) for row in rows[(num % 3)]
                    [(2 - params[reagent]['offset'] if params[reagent]['skip'] != 1 and (num % 3) == 1 else params[reagent]['offset'])::params[reagent]['skip']]
                ] for reagent in reagents
            )
        )
    )

    dispense = True

    ch_a = False
    ch_b = True
    ch_c = False
    magnetic_beads = True
    tracer = True
    wash_buffer = True
    tmb = True

    #specify from 1 - 20 how many cartidges are to be filled
    cartridges_per_deck = 20

    tube_locations = {
        'ch_a': 'C2',
        'ch_b': 'C2',
        'ch_c': 'C2',
        'magnetic_beads': 'A4',
        'tracer': 'C1',
        'wash_buffer': 'B2',
        'tmb': 'A2',
    }
    dead_volume = 50
    wells_per_cartridge = 3

## old volumes: calibrator - 28, beads - 18, tracer - 18, WB - 18, TMB - 28 ; new volumes calibrator - 21, beads - 20, tracer - 20, WB - 20, TMB - 32
    well_volumes = {
        'ch_a': 28,
        'ch_b': 20,
        'ch_c': 28,
        'magnetic_beads': 20,
        'tracer': 20,
        'wash_buffer': 20,
        'tmb': 32,
    }

    if dispense:
        if magnetic_beads:
            adjust = pick_up_and_calibrate_tip()
            dispense_reagent([tube_locations['magnetic_beads']], destinations['magnetic_beads'], well_volume=well_volumes['magnetic_beads'], source_volume= dead_volume + (well_volumes['magnetic_beads'] * wells_per_cartridge * cartridges_per_deck), adjust=adjust, cartridges_per_deck = cartridges_per_deck)

        if tracer:
            adjust = pick_up_and_calibrate_tip()
            dispense_reagent([tube_locations['tracer']], destinations['tracer'], well_volume=well_volumes['tracer'], source_volume= dead_volume + (well_volumes['tracer'] * wells_per_cartridge * cartridges_per_deck), adjust=adjust, cartridges_per_deck = cartridges_per_deck)

        if wash_buffer:
            adjust = pick_up_and_calibrate_tip()
            dispense_reagent([tube_locations['wash_buffer']], destinations['wash_buffer'], well_volume=well_volumes['wash_buffer'], source_volume= dead_volume + (well_volumes['wash_buffer'] * wells_per_cartridge * cartridges_per_deck), adjust=adjust, cartridges_per_deck = cartridges_per_deck)

        if tmb:
            adjust = pick_up_and_calibrate_tip()
            dispense_reagent([tube_locations['tmb']], destinations['tmb'], well_volume=well_volumes['tmb'], source_volume= dead_volume + (well_volumes['tmb'] * wells_per_cartridge * cartridges_per_deck), adjust=adjust, cartridges_per_deck = cartridges_per_deck)
 
        if ch_c:
            adjust = pick_up_and_calibrate_tip()
            dispense_reagent([tube_locations['ch_c']], destinations['ch_c'], well_volume=well_volumes['ch_c'], source_volume= dead_volume + (well_volumes['ch_c'] * cartridges_per_deck), adjust=adjust, cartridges_per_deck = cartridges_per_deck)

        if ch_b:
            adjust = pick_up_and_calibrate_tip()
            dispense_reagent([tube_locations['ch_b']], destinations['ch_b'], well_volume=well_volumes['ch_b'], source_volume= dead_volume + (well_volumes['ch_b'] * cartridges_per_deck), adjust=adjust, cartridges_per_deck = cartridges_per_deck)

        if ch_a:
            adjust = pick_up_and_calibrate_tip()
            dispense_reagent([tube_locations['ch_a']], destinations['ch_a'], well_volume=well_volumes['ch_a'], source_volume= dead_volume + (well_volumes['ch_a'] * cartridges_per_deck), adjust=adjust, cartridges_per_deck = cartridges_per_deck)

    ser.close()
